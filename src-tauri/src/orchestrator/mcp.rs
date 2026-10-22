//! The orchestrator's HTTP MCP endpoint: JSON-RPC 2.0 over `POST /mcp`, bearer-token
//! authenticated, with the tools from `tools`. A small HTTP/1.1 server on raw TCP (as in
//! `providers::mcp_host`) that supports keep-alive, because MCP clients reuse connections.
use super::tools;
use crate::{AppHandle, Result};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::time::Duration;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{OwnedSemaphorePermit, Semaphore};
use tokio::task::JoinSet;

const HEADER_LIMIT: usize = 16 * 1024;
const BODY_LIMIT: usize = 1024 * 1024;
const MAX_CONNECTIONS: usize = 32;
/// Time to receive the rest of a request once its first bytes arrived.
const READ_TIMEOUT: Duration = Duration::from_secs(10);
/// How long a keep-alive connection may wait for its next request.
const IDLE_TIMEOUT: Duration = Duration::from_secs(30);
const WRITE_TIMEOUT: Duration = Duration::from_secs(10);
const LATEST_VERSION: &str = "2025-06-18";
const VERSIONS: [&str; 3] = ["2025-06-18", "2025-03-26", "2024-11-05"];

type Boxed<T> = Pin<Box<dyn Future<Output = T> + Send>>;
type TokenFn = Arc<dyn Fn() -> Option<String> + Send + Sync>;

/// What the server exposes: the tool catalog and tool calls.
trait Dispatcher: Send + Sync + 'static {
    fn catalog(&self) -> Vec<Value>;
    fn call(&self, name: String, args: Value) -> Boxed<Result<Value>>;
    /// A one-line summary written before the JSON in a tool result's text.
    fn summary(&self, _name: &str, _result: &Value) -> Option<String> {
        None
    }
}

/// The real tools. A call runs as its own task so closing the connection or stopping the
/// endpoint never abandons a half-finished change.
struct Tools(AppHandle);
impl Dispatcher for Tools {
    fn catalog(&self) -> Vec<Value> {
        tools::catalog()
    }
    fn summary(&self, name: &str, result: &Value) -> Option<String> {
        Some(tools::summary(name, result))
    }
    fn call(&self, name: String, args: Value) -> Boxed<Result<Value>> {
        let app = self.0.clone();
        Box::pin(async move {
            tauri::async_runtime::spawn(async move { tools::call(&app, &name, args).await })
                .await
                .map_err(|e| format!("The tool stopped unexpectedly: {e}"))?
        })
    }
}

/// A running endpoint; `stop` closes its listener.
pub struct Handle {
    task: tauri::async_runtime::JoinHandle<()>,
    closed: std::sync::mpsc::Receiver<()>,
}
impl Handle {
    /// Closes the listener and every connection. Returns once the port is free again.
    pub fn stop(self) {
        self.task.abort();
        let _ = self.closed.recv_timeout(Duration::from_secs(2));
    }
}

/// The listener; dropping it (inner first) also disconnects `_closed`, which `stop` waits for.
struct Bound {
    inner: TcpListener,
    _closed: std::sync::mpsc::Sender<()>,
}

/// Serves `POST /mcp` on `address` until stopped.
pub fn start(app: AppHandle, address: std::net::SocketAddr) -> Result<Handle> {
    let listener = bind(address)?;
    Ok(spawn(listener, Arc::new(|| super::token(false)), Arc::new(Tools(app))))
}

fn bind(address: std::net::SocketAddr) -> Result<std::net::TcpListener> {
    let listener = std::net::TcpListener::bind(address).map_err(|e| format!("Could not listen on {address}: {e}"))?;
    listener.set_nonblocking(true).map_err(|e| e.to_string())?;
    Ok(listener)
}

fn spawn(listener: std::net::TcpListener, token: TokenFn, dispatcher: Arc<dyn Dispatcher>) -> Handle {
    let (sender, closed) = std::sync::mpsc::channel();
    let task = tauri::async_runtime::spawn(async move {
        let Ok(inner) = TcpListener::from_std(listener) else { return };
        serve(Bound { inner, _closed: sender }, token, dispatcher).await;
    });
    Handle { task, closed }
}

async fn serve(listener: Bound, token: TokenFn, dispatcher: Arc<dyn Dispatcher>) {
    let permits = Arc::new(Semaphore::new(MAX_CONNECTIONS));
    // Dropping the set (when the server is aborted) drops every connection with it.
    let mut connections = JoinSet::new();
    loop {
        tokio::select! {
            accepted = listener.inner.accept() => match accepted {
                Ok((stream, _)) => match permits.clone().try_acquire_owned() {
                    Ok(permit) => {
                        connections.spawn(connection(stream, permit, token.clone(), dispatcher.clone()));
                    }
                    Err(_) => {
                        connections.spawn(refuse(stream));
                    }
                },
                Err(_) => tokio::time::sleep(Duration::from_millis(50)).await,
            },
            Some(_) = connections.join_next(), if !connections.is_empty() => {}
        }
    }
}

async fn refuse(mut stream: TcpStream) {
    let _ = tokio::time::timeout(WRITE_TIMEOUT, respond(&mut stream, 503, &[], b"", true)).await;
}

struct Head {
    method: String,
    path: String,
    headers: HashMap<String, String>,
    close: bool,
}

async fn connection(mut stream: TcpStream, _permit: OwnedSemaphorePermit, token: TokenFn, dispatcher: Arc<dyn Dispatcher>) {
    let mut buf = Vec::new();
    loop {
        let (status, extra, body, close, session) = match exchange(&mut stream, &mut buf, &token, dispatcher.as_ref()).await {
            Ok(None) => return,
            Ok(Some(reply)) => reply,
            Err(status) => (status, if status == 405 { vec![("Allow", "POST".to_owned())] } else { Vec::new() }, Vec::new(), true, false),
        };
        let mut headers = extra;
        if session {
            headers.push(("Mcp-Session-Id", uuid::Uuid::new_v4().to_string()));
        }
        let written = tokio::time::timeout(WRITE_TIMEOUT, respond(&mut stream, status, &headers, &body, close)).await;
        if close || !matches!(written, Ok(Ok(()))) {
            return;
        }
    }
}

type Reply = (u16, Vec<(&'static str, String)>, Vec<u8>, bool, bool);

/// Reads one request and answers it. `Ok(None)` when the peer went away between requests;
/// `Err(status)` for a refusal, after which the connection closes.
async fn exchange(stream: &mut TcpStream, buf: &mut Vec<u8>, token: &TokenFn, dispatcher: &dyn Dispatcher) -> std::result::Result<Option<Reply>, u16> {
    let Some(head) = read_head(stream, buf).await? else { return Ok(None) };
    // Checked before the body is read, let alone parsed.
    if head.headers.contains_key("origin") {
        return Err(403);
    }
    if !authorized(head.headers.get("authorization").map(String::as_str), token().as_deref()) {
        return Err(401);
    }
    if head.path != "/mcp" {
        return Err(404);
    }
    if head.method != "POST" {
        return Err(405);
    }
    if head.headers.contains_key("transfer-encoding") {
        return Err(400);
    }
    let length = head.headers.get("content-length").ok_or(411u16)?.parse::<usize>().map_err(|_| 400u16)?;
    if length > BODY_LIMIT {
        return Err(413);
    }
    if head.headers.get("expect").is_some_and(|v| v.eq_ignore_ascii_case("100-continue")) && buf.len() < length {
        let _ = tokio::time::timeout(WRITE_TIMEOUT, stream.write_all(b"HTTP/1.1 100 Continue\r\n\r\n")).await;
    }
    let body = tokio::time::timeout(READ_TIMEOUT, async {
        while buf.len() < length {
            if fill(stream, buf).await.map_err(|_| 400u16)? == 0 {
                return Err(400u16);
            }
        }
        Ok(buf.drain(..length).collect::<Vec<u8>>())
    })
    .await
    .map_err(|_| 408u16)??;
    let (response, session) = process(dispatcher, &body).await;
    Ok(Some(match response {
        Some(value) => (200, vec![("Content-Type", "application/json".into())], serde_json::to_vec(&value).unwrap_or_default(), head.close, session),
        None => (202, Vec::new(), Vec::new(), head.close, false),
    }))
}

async fn fill(stream: &mut TcpStream, buf: &mut Vec<u8>) -> std::io::Result<usize> {
    let mut chunk = [0; 8192];
    let count = stream.read(&mut chunk).await?;
    buf.extend_from_slice(&chunk[..count]);
    Ok(count)
}

/// Reads and parses a request line and headers, leaving any bytes after them in `buf`.
async fn read_head(stream: &mut TcpStream, buf: &mut Vec<u8>) -> std::result::Result<Option<Head>, u16> {
    if buf.is_empty() && !matches!(tokio::time::timeout(IDLE_TIMEOUT, fill(stream, buf)).await, Ok(Ok(count)) if count > 0) {
        return Ok(None);
    }
    let end = tokio::time::timeout(READ_TIMEOUT, async {
        loop {
            if let Some(index) = buf.windows(4).position(|w| w == b"\r\n\r\n") {
                return if index + 4 > HEADER_LIMIT { Err(431u16) } else { Ok(index + 4) };
            }
            if buf.len() >= HEADER_LIMIT {
                return Err(431);
            }
            if fill(stream, buf).await.map_err(|_| 400u16)? == 0 {
                return Err(400);
            }
        }
    })
    .await
    .map_err(|_| 408u16)??;
    let head = parse_head(&buf[..end]).ok_or(400u16);
    buf.drain(..end);
    head.map(Some)
}

fn parse_head(bytes: &[u8]) -> Option<Head> {
    let text = std::str::from_utf8(bytes).ok()?;
    let mut lines = text.split("\r\n");
    let mut request = lines.next()?.split(' ');
    let method = request.next()?.to_owned();
    let target = request.next()?;
    let version = request.next()?;
    if request.next().is_some() || !matches!(version, "HTTP/1.1" | "HTTP/1.0") {
        return None;
    }
    let mut headers = HashMap::new();
    for line in lines.filter(|line| !line.is_empty()) {
        let (name, value) = line.split_once(':')?;
        if name.is_empty() || !name.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-') {
            return None;
        }
        // A repeated header could hide a second credential or length from a proxy.
        if headers.insert(name.to_ascii_lowercase(), value.trim().to_owned()).is_some() {
            return None;
        }
    }
    let connection = headers.get("connection").map(|v| v.to_ascii_lowercase()).unwrap_or_default();
    let close = connection.split(',').any(|v| v.trim() == "close") || (version == "HTTP/1.0" && !connection.contains("keep-alive"));
    let path = target.split('?').next().unwrap_or_default().to_owned();
    Some(Head { method, path, headers, close })
}

fn authorized(header: Option<&str>, token: Option<&str>) -> bool {
    let (Some(header), Some(token)) = (header, token) else { return false };
    let Some((scheme, given)) = header.split_once(' ') else { return false };
    scheme.eq_ignore_ascii_case("bearer") && constant_time_eq(given.trim().as_bytes(), token.as_bytes())
}

/// Compares without stopping at the first difference.
fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    let mut diff = a.len() ^ b.len();
    for index in 0..a.len().max(b.len()) {
        diff |= (a.get(index).copied().unwrap_or(0) ^ b.get(index).copied().unwrap_or(0)) as usize;
    }
    diff == 0
}

async fn respond(stream: &mut TcpStream, status: u16, headers: &[(&str, String)], body: &[u8], close: bool) -> std::io::Result<()> {
    let reason = match status {
        200 => "OK",
        202 => "Accepted",
        400 => "Bad Request",
        401 => "Unauthorized",
        403 => "Forbidden",
        404 => "Not Found",
        405 => "Method Not Allowed",
        408 => "Request Timeout",
        411 => "Length Required",
        413 => "Payload Too Large",
        431 => "Request Header Fields Too Large",
        503 => "Service Unavailable",
        _ => "Error",
    };
    let mut head = format!("HTTP/1.1 {status} {reason}\r\nContent-Length: {}\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nConnection: {}\r\n", body.len(), if close { "close" } else { "keep-alive" });
    for (name, value) in headers {
        head.push_str(&format!("{name}: {value}\r\n"));
    }
    head.push_str("\r\n");
    stream.write_all(head.as_bytes()).await?;
    stream.write_all(body).await?;
    stream.flush().await
}

/// Answers a JSON-RPC body: `None` when nothing is owed (notifications), and whether
/// the client initialized, which starts a session.
async fn process(dispatcher: &dyn Dispatcher, body: &[u8]) -> (Option<Value>, bool) {
    let Ok(message) = serde_json::from_slice::<Value>(body) else {
        return (Some(failure(Value::Null, -32700, "Parse error")), false);
    };
    let mut initialized = false;
    let response = match message {
        Value::Array(items) if items.is_empty() => Some(failure(Value::Null, -32600, "Invalid Request")),
        Value::Array(items) => {
            let mut responses = Vec::new();
            for item in items {
                responses.extend(handle(dispatcher, item, &mut initialized).await);
            }
            (!responses.is_empty()).then(|| Value::Array(responses))
        }
        item => handle(dispatcher, item, &mut initialized).await,
    };
    (response, initialized)
}

fn failure(id: Value, code: i64, message: &str) -> Value {
    json!({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}})
}

async fn handle(dispatcher: &dyn Dispatcher, message: Value, initialized: &mut bool) -> Option<Value> {
    let Some(object) = message.as_object() else { return Some(failure(Value::Null, -32600, "Invalid Request")) };
    let id = object.get("id").cloned();
    let Some(method) = object.get("method").and_then(Value::as_str) else {
        // A response from the client; there is nothing to answer.
        if object.contains_key("result") || object.contains_key("error") {
            return None;
        }
        return Some(failure(id.unwrap_or(Value::Null), -32600, "Invalid Request"));
    };
    let id = id?;
    let params = object.get("params").cloned().unwrap_or(Value::Null);
    let result = match method {
        "initialize" => {
            *initialized = true;
            let asked = params["protocolVersion"].as_str().unwrap_or_default();
            let version = VERSIONS.iter().find(|v| **v == asked).copied().unwrap_or(LATEST_VERSION);
            Ok(json!({
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": false}},
                "serverInfo": {"name": "pandacode", "version": env!("CARGO_PKG_VERSION")},
            }))
        }
        "ping" => Ok(json!({})),
        "tools/list" => Ok(json!({"tools": dispatcher.catalog()})),
        "tools/call" => call_tool(dispatcher, &params).await,
        _ => Err((-32601, format!("Method not found: {method}"))),
    };
    Some(match result {
        Ok(result) => json!({"jsonrpc": "2.0", "id": id, "result": result}),
        Err((code, message)) => failure(id, code, &message),
    })
}

async fn call_tool(dispatcher: &dyn Dispatcher, params: &Value) -> std::result::Result<Value, (i64, String)> {
    let invalid = || (-32602, "tools/call needs a tool name and object arguments.".to_owned());
    let name = params["name"].as_str().ok_or_else(invalid)?;
    let args = match params.get("arguments") {
        None | Some(Value::Null) => json!({}),
        Some(args) if args.is_object() => args.clone(),
        Some(_) => return Err(invalid()),
    };
    Ok(match dispatcher.call(name.to_owned(), args).await {
        Ok(value) => {
            let text = match dispatcher.summary(name, &value) {
                Some(summary) => format!("{summary}\n{value}"),
                None => value.to_string(),
            };
            let mut result = json!({"content": [{"type": "text", "text": text}], "isError": false});
            if value.is_object() {
                result["structuredContent"] = value;
            }
            result
        }
        Err(message) => json!({"content": [{"type": "text", "text": message}], "isError": true}),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Fake;
    impl Dispatcher for Fake {
        fn catalog(&self) -> Vec<Value> {
            vec![json!({"name": "echo", "description": "Echo", "inputSchema": {"type": "object"}})]
        }
        fn call(&self, name: String, args: Value) -> Boxed<Result<Value>> {
            Box::pin(async move {
                match name.as_str() {
                    "echo" => Ok(args),
                    "text" => Ok(json!("hello")),
                    _ => Err(format!("Unknown tool {name}.")),
                }
            })
        }
    }

    struct Reply {
        status: u16,
        headers: HashMap<String, String>,
        body: Value,
    }

    struct Server {
        address: std::net::SocketAddr,
        handle: Handle,
    }
    fn server(token: Option<&'static str>) -> Server {
        let listener = bind("127.0.0.1:0".parse().unwrap()).unwrap();
        let address = listener.local_addr().unwrap();
        let handle = spawn(listener, Arc::new(move || token.map(String::from)), Arc::new(Fake));
        Server { address, handle }
    }

    fn post(token: Option<&str>, extra: &str, body: &[u8]) -> Vec<u8> {
        let auth = token.map(|t| format!("Authorization: Bearer {t}\r\n")).unwrap_or_default();
        let mut bytes = format!("POST /mcp HTTP/1.1\r\nHost: x\r\n{auth}{extra}Content-Type: application/json\r\nContent-Length: {}\r\n\r\n", body.len()).into_bytes();
        bytes.extend_from_slice(body);
        bytes
    }
    fn rpc(body: Value) -> Vec<u8> {
        post(Some("secret"), "", &serde_json::to_vec(&body).unwrap())
    }

    async fn read_reply(stream: &mut TcpStream) -> Reply {
        read_buffered(stream, &mut Vec::new()).await
    }

    /// Reads one response, leaving bytes of any following response in `bytes`.
    async fn read_buffered(stream: &mut TcpStream, bytes: &mut Vec<u8>) -> Reply {
        let end = loop {
            if let Some(index) = bytes.windows(4).position(|w| w == b"\r\n\r\n") {
                break index + 4;
            }
            let mut chunk = [0; 1024];
            let count = tokio::time::timeout(Duration::from_secs(5), stream.read(&mut chunk)).await.unwrap().unwrap();
            assert!(count > 0, "connection closed before a full response");
            bytes.extend_from_slice(&chunk[..count]);
        };
        let head = String::from_utf8(bytes[..end].to_vec()).unwrap();
        let mut lines = head.split("\r\n");
        let status = lines.next().unwrap().split(' ').nth(1).unwrap().parse().unwrap();
        let headers: HashMap<String, String> = lines.filter_map(|l| l.split_once(": ")).map(|(k, v)| (k.to_ascii_lowercase(), v.to_owned())).collect();
        let length: usize = headers["content-length"].parse().unwrap();
        while bytes.len() < end + length {
            let mut chunk = [0; 1024];
            let count = tokio::time::timeout(Duration::from_secs(5), stream.read(&mut chunk)).await.unwrap().unwrap();
            assert!(count > 0, "connection closed before the full body");
            bytes.extend_from_slice(&chunk[..count]);
        }
        let body = serde_json::from_slice(&bytes[end..end + length]).unwrap_or(Value::Null);
        bytes.drain(..end + length);
        Reply { status, headers, body }
    }

    async fn send(server: &Server, request: Vec<u8>) -> Reply {
        let mut stream = TcpStream::connect(server.address).await.unwrap();
        stream.write_all(&request).await.unwrap();
        read_reply(&mut stream).await
    }

    #[tokio::test]
    async fn initialize_echoes_known_versions_and_starts_a_session() {
        let server = server(Some("secret"));
        for version in ["2025-06-18", "2025-03-26", "2024-11-05"] {
            let reply = send(&server, rpc(json!({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": version}}))).await;
            assert_eq!(reply.status, 200);
            assert_eq!(reply.headers["content-type"], "application/json");
            assert!(reply.headers["mcp-session-id"].len() >= 32);
            assert_eq!(reply.body["id"], 1);
            assert_eq!(reply.body["result"]["protocolVersion"], version);
            assert_eq!(reply.body["result"]["capabilities"]["tools"]["listChanged"], false);
            assert_eq!(reply.body["result"]["serverInfo"]["name"], "pandacode");
            assert_eq!(reply.body["result"]["serverInfo"]["version"], env!("CARGO_PKG_VERSION"));
        }
        let reply = send(&server, rpc(json!({"jsonrpc": "2.0", "id": "a", "method": "initialize", "params": {"protocolVersion": "1999-01-01"}}))).await;
        assert_eq!(reply.body["id"], "a");
        assert_eq!(reply.body["result"]["protocolVersion"], "2025-06-18");
        let reply = send(&server, rpc(json!({"jsonrpc": "2.0", "id": 2, "method": "ping"}))).await;
        assert_eq!(reply.body["result"], json!({}));
        assert!(!reply.headers.contains_key("mcp-session-id"));
        server.handle.stop();
    }

    #[tokio::test]
    async fn notifications_get_202_with_no_body() {
        let server = server(Some("secret"));
        let reply = send(&server, rpc(json!({"jsonrpc": "2.0", "method": "notifications/initialized"}))).await;
        assert_eq!(reply.status, 202);
        assert_eq!(reply.body, Value::Null);
        assert_eq!(reply.headers["content-length"], "0");
        server.handle.stop();
    }

    #[tokio::test]
    async fn tools_list_and_call() {
        let server = server(Some("secret"));
        let listed = send(&server, rpc(json!({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}))).await;
        assert_eq!(listed.body["result"]["tools"][0]["name"], "echo");

        let call = |name: &str, args: Value| rpc(json!({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": args}}));
        let ok = send(&server, call("echo", json!({"a": 1}))).await;
        assert_eq!(ok.body["result"]["isError"], false);
        assert_eq!(ok.body["result"]["content"][0]["type"], "text");
        assert_eq!(ok.body["result"]["content"][0]["text"], "{\"a\":1}");
        assert_eq!(ok.body["result"]["structuredContent"], json!({"a": 1}));

        let scalar = send(&server, call("text", json!({}))).await;
        assert_eq!(scalar.body["result"]["content"][0]["text"], "\"hello\"");
        assert!(scalar.body["result"].get("structuredContent").is_none());

        let failed = send(&server, call("nope", json!({}))).await;
        assert_eq!(failed.status, 200);
        assert!(failed.body.get("error").is_none());
        assert_eq!(failed.body["result"]["isError"], true);
        assert_eq!(failed.body["result"]["content"][0]["text"], "Unknown tool nope.");

        let missing = send(&server, rpc(json!({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {}}))).await;
        assert_eq!(missing.body["error"]["code"], -32602);
        let bad_args = send(&server, call("echo", json!([1]))).await;
        assert_eq!(bad_args.body["error"]["code"], -32602);
        server.handle.stop();
    }

    #[tokio::test]
    async fn bad_json_and_unknown_methods_are_json_rpc_errors() {
        let server = server(Some("secret"));
        let reply = send(&server, post(Some("secret"), "", b"{not json")).await;
        assert_eq!(reply.status, 200);
        assert_eq!(reply.body["error"]["code"], -32700);
        assert_eq!(reply.body["id"], Value::Null);
        let reply = send(&server, rpc(json!({"jsonrpc": "2.0", "id": 9, "method": "resources/list"}))).await;
        assert_eq!(reply.body["id"], 9);
        assert_eq!(reply.body["error"]["code"], -32601);
        server.handle.stop();
    }

    #[tokio::test]
    async fn batches_are_answered_element_by_element() {
        let server = server(Some("secret"));
        let reply = send(&server, rpc(json!([
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "nope"},
        ])))
        .await;
        let items = reply.body.as_array().unwrap();
        assert_eq!(items.len(), 2);
        assert_eq!(items[0]["result"], json!({}));
        assert_eq!(items[1]["error"]["code"], -32601);
        let reply = send(&server, rpc(json!([{"jsonrpc": "2.0", "method": "notifications/initialized"}]))).await;
        assert_eq!(reply.status, 202);
        server.handle.stop();
    }

    #[tokio::test]
    async fn requests_without_the_right_token_are_refused_before_the_body_is_read() {
        let server = server(Some("secret"));
        let body = serde_json::to_vec(&json!({"jsonrpc": "2.0", "id": 1, "method": "ping"})).unwrap();
        assert_eq!(send(&server, post(None, "", &body)).await.status, 401);
        assert_eq!(send(&server, post(Some("wrong"), "", &body)).await.status, 401);
        assert_eq!(send(&server, post(Some("secre"), "", &body)).await.status, 401);
        assert_eq!(send(&server, post(Some("secret!"), "", &body)).await.status, 401);
        // Garbage that is not JSON gets 401, not a parse error, without a token.
        assert_eq!(send(&server, post(Some("wrong"), "", b"{")).await.status, 401);
        assert_eq!(send(&server, post(Some("secret"), "Origin: http://evil.example\r\n", &body)).await.status, 403);
        assert_eq!(send(&server, post(None, "Origin: http://evil.example\r\n", &body)).await.status, 403);
        assert_eq!(send(&server, post(Some("secret"), "", &body)).await.status, 200);
        server.handle.stop();
    }

    #[tokio::test]
    async fn every_request_is_refused_while_no_token_exists() {
        let server = server(None);
        let body = serde_json::to_vec(&json!({"jsonrpc": "2.0", "id": 1, "method": "ping"})).unwrap();
        assert_eq!(send(&server, post(Some("secret"), "", &body)).await.status, 401);
        assert_eq!(send(&server, post(Some(""), "", &body)).await.status, 401);
        assert_eq!(send(&server, post(None, "", &body)).await.status, 401);
        server.handle.stop();
    }

    #[tokio::test]
    async fn limits_and_routes() {
        let server = server(Some("secret"));
        let mut oversized = b"POST /mcp HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer secret\r\nContent-Length: 2000000\r\n\r\n".to_vec();
        oversized.extend_from_slice(b"{}");
        assert_eq!(send(&server, oversized).await.status, 413);

        for method in ["GET", "DELETE"] {
            let reply = send(&server, format!("{method} /mcp HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer secret\r\n\r\n").into_bytes()).await;
            assert_eq!(reply.status, 405);
            assert_eq!(reply.headers["allow"], "POST");
        }
        let reply = send(&server, b"POST /other HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer secret\r\nContent-Length: 2\r\n\r\n{}".to_vec()).await;
        assert_eq!(reply.status, 404);
        let reply = send(&server, b"POST /mcp HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer secret\r\n\r\n".to_vec()).await;
        assert_eq!(reply.status, 411);
        let mut huge_header = b"POST /mcp HTTP/1.1\r\nHost: x\r\nX-Pad: ".to_vec();
        huge_header.extend(std::iter::repeat(b'a').take(20 * 1024));
        huge_header.extend_from_slice(b"\r\n\r\n");
        assert_eq!(send(&server, huge_header).await.status, 431);
        server.handle.stop();
    }

    #[tokio::test]
    async fn one_connection_serves_several_requests_until_close() {
        let server = server(Some("secret"));
        let mut stream = TcpStream::connect(server.address).await.unwrap();
        stream.write_all(&rpc(json!({"jsonrpc": "2.0", "id": 1, "method": "ping"}))).await.unwrap();
        let first = read_reply(&mut stream).await;
        assert_eq!(first.body["id"], 1);
        assert_eq!(first.headers["connection"], "keep-alive");
        // Two pipelined requests arriving in one write.
        let mut both = rpc(json!({"jsonrpc": "2.0", "id": 2, "method": "ping"}));
        both.extend(rpc(json!({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})));
        stream.write_all(&both).await.unwrap();
        let mut pending = Vec::new();
        assert_eq!(read_buffered(&mut stream, &mut pending).await.body["id"], 2);
        assert_eq!(read_buffered(&mut stream, &mut pending).await.body["id"], 3);
        stream.write_all(&post(Some("secret"), "Connection: close\r\n", br#"{"jsonrpc":"2.0","id":4,"method":"ping"}"#)).await.unwrap();
        let last = read_reply(&mut stream).await;
        assert_eq!(last.body["id"], 4);
        assert_eq!(last.headers["connection"], "close");
        let mut rest = Vec::new();
        assert_eq!(stream.read_to_end(&mut rest).await.unwrap(), 0);
        server.handle.stop();
    }

    #[tokio::test]
    async fn stop_frees_the_port_and_closes_connections() {
        let server = server(Some("secret"));
        let mut stream = TcpStream::connect(server.address).await.unwrap();
        stream.write_all(&rpc(json!({"jsonrpc": "2.0", "id": 1, "method": "ping"}))).await.unwrap();
        read_reply(&mut stream).await;
        server.handle.stop();
        std::net::TcpListener::bind(server.address).expect("the port is free right after stop");
        let mut rest = Vec::new();
        let closed = tokio::time::timeout(Duration::from_secs(2), stream.read_to_end(&mut rest)).await;
        assert!(matches!(closed, Ok(Ok(0)) | Ok(Err(_))));
    }

    #[test]
    fn start_reports_an_address_in_use() {
        let taken = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let error = bind(taken.local_addr().unwrap()).unwrap_err();
        assert!(error.contains("Could not listen"), "{error}");
    }

    #[test]
    fn tokens_compare_exactly() {
        assert!(constant_time_eq(b"abc", b"abc"));
        assert!(!constant_time_eq(b"abc", b"abd"));
        assert!(!constant_time_eq(b"abc", b"abcd"));
        assert!(!constant_time_eq(b"", b"a"));
        assert!(authorized(Some("bearer abc"), Some("abc")));
        assert!(!authorized(Some("Basic abc"), Some("abc")));
        assert!(!authorized(Some("Bearer abc"), None));
    }
}
