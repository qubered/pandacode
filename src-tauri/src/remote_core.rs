//! The headless core for remote mode (docs/remote-core.md): the same commands and
//! events as the desktop app, served over a WebSocket instead of a webview.
//!
//! Frames are JSON. A client sends `{id, command, args}` and `{subscribe: [names]}`;
//! the core answers `{id, ok, result | error}` and pushes `{event, payload}` for the
//! events a client subscribed to. Commands run through `crate::invoke_handler` on a
//! windowless "main" webview of Tauri's mock runtime, so argument parsing, managed
//! state and the main-window checks are exactly the desktop app's.

use crate::Rt;
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use std::{collections::HashSet, net::SocketAddr, path::PathBuf, sync::Arc, time::Duration};
use tauri::ipc::{CallbackFn, InvokeBody, InvokeResponse, InvokeResponseBody};
use tauri::webview::InvokeRequest;
use tauri::{Listener, Manager, Webview};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{broadcast, mpsc};
use tokio_tungstenite::tungstenite::Message;

/// Bundle identifier of the desktop app: the headless core reads and writes the same data.
const IDENTIFIER: &str = "ai.pandacode.desktop";

/// Every event channel the core emits. `channels_cover_every_emit` checks this list
/// against the Rust emitters and the frontend's subscriptions.
pub const CHANNELS: &[&str] = &[
    "agent-event",
    "agent-ui-action",
    "agent-update-progress",
    "android-setup",
    "architect-event",
    "bot-notification",
    "bot-pairing",
    "bots-changed",
    "cloud-setup",
    "jev-frame",
    "jev-install",
    "jev-run",
    "provider-models-changed",
    "queue-changed",
    "sessions-changed",
    "sessions-deleted",
    "simulator-changed",
    "terminal-event",
    "vm-changed",
];

/// Commands that need the desktop shell or the host's display (docs/remote-core.md §3).
/// The headless core refuses them up front with a clear message.
fn unavailable(command: &str) -> bool {
    const PREFIXES: &[&str] = &["browser_", "overlay_", "simulator_", "vm_", "cloud_", "jev_"];
    PREFIXES.iter().any(|prefix| command.starts_with(prefix))
        || matches!(command, "android_setup" | "prepare_file_preview")
}

/// Commands the headless core answers itself instead of the desktop handler.
fn local_answer(command: &str) -> Option<Value> {
    match command {
        // Window materials belong to the desktop shell; a browser draws solid surfaces.
        "configure_window_appearance" => Some(json!({ "platform": "other", "material": "solid" })),
        _ => None,
    }
}

pub struct Options {
    pub address: SocketAddr,
    pub data_dir: Option<PathBuf>,
    /// Extra page origins (such as `http://laptop:1420`) allowed to connect.
    pub allowed_origins: Vec<String>,
}

/// Which browser pages may connect. There is no login: the tailnet is the boundary.
/// But any page open in a browser inside that boundary can open a WebSocket to the
/// core, and browsers apply no CORS to WebSockets. So a connection that carries an
/// `Origin` (every browser's does) must come from a frontend on loopback or from an
/// origin the operator listed. Clients without an `Origin` (scripts, tests) pass.
#[derive(Clone, Default)]
pub(crate) struct Origins(Arc<Vec<String>>);
impl Origins {
    pub(crate) fn new(extra: Vec<String>) -> Self {
        Self(Arc::new(extra.into_iter().map(|o| o.trim_end_matches('/').to_ascii_lowercase()).collect()))
    }
    fn allows(&self, origin: Option<&str>) -> bool {
        let Some(origin) = origin else { return true };
        let origin = origin.trim_end_matches('/').to_ascii_lowercase();
        if self.0.iter().any(|allowed| *allowed == origin) {
            return true;
        }
        let Ok(url) = tauri::Url::parse(&origin) else { return false };
        matches!(url.scheme(), "http" | "https")
            && matches!(url.host_str(), Some("localhost" | "127.0.0.1" | "[::1]"))
    }
}

pub(crate) struct Core {
    app: tauri::App<Rt>,
    webview: Webview<Rt>,
    events: broadcast::Sender<(&'static str, Arc<str>)>,
    dir: PathBuf,
}

/// The desktop app's state and commands on a windowless "main" webview.
pub(crate) fn start(data_dir: Option<PathBuf>) -> Result<Core, String> {
    let mut context = tauri::test::mock_context(tauri::test::noop_assets());
    context.config_mut().identifier = IDENTIFIER.into();
    let app = tauri::test::mock_builder()
        .invoke_handler(crate::invoke_handler)
        .build(context)
        .map_err(|e| format!("Could not start the core: {e}"))?;
    let handle = app.handle().clone();
    let dir = match data_dir {
        Some(dir) => dir,
        None => handle.path().app_data_dir().map_err(|e| e.to_string())?,
    };
    crate::secret_store::file::use_folder(&dir);
    crate::setup_core(&handle, &dir).map_err(|e| format!("Could not open {}: {e}", dir.display()))?;
    let webview = tauri::WebviewWindowBuilder::new(&app, "main", Default::default())
        .build()
        .map_err(|e| e.to_string())?;
    let webview: Webview<Rt> = webview.as_ref().clone();
    let events = forward_events(&handle);
    Ok(Core { app, webview, events, dir })
}

/// Starts the core and serves clients until SIGINT/SIGTERM.
pub fn serve(options: Options) -> Result<(), String> {
    let Core { app, webview, events, dir } = start(options.data_dir)?;
    let origins = Origins::new(options.allowed_origins);
    let handle = app.handle().clone();
    eprintln!("PandaCode core: data in {}", dir.display());
    tauri::async_runtime::block_on(async move {
        let listener = TcpListener::bind(options.address)
            .await
            .map_err(|e| format!("Could not listen on {}: {e}", options.address))?;
        eprintln!("PandaCode core: listening on ws://{}", listener.local_addr().map_err(|e| e.to_string())?);
        tokio::select! {
            result = accept(listener, webview, events, origins) => result,
            _ = terminated() => {
                eprintln!("PandaCode core: stopping agents…");
                if crate::begin_shutdown(&handle) {
                    crate::finish_shutdown(handle.clone()).await;
                }
                crate::release_resources(&handle);
                Ok(())
            }
        }
    })?;
    drop(app);
    Ok(())
}

/// Every event becomes one ready-to-send frame on a broadcast channel.
fn forward_events(app: &crate::AppHandle) -> broadcast::Sender<(&'static str, Arc<str>)> {
    // Generous: terminals stream fast. A client that still falls behind is disconnected
    // and reloads everything when it reconnects.
    let (tx, _) = broadcast::channel(8192);
    for &channel in CHANNELS {
        let tx = tx.clone();
        app.listen_any(channel, move |event| {
            let frame = format!(r#"{{"event":{},"payload":{}}}"#, json!(channel), payload(event.payload()));
            let _ = tx.send((channel, frame.into()));
        });
    }
    tx
}
fn payload(raw: &str) -> &str {
    if raw.is_empty() { "null" } else { raw }
}

pub(crate) async fn accept(
    listener: TcpListener,
    webview: Webview<Rt>,
    events: broadcast::Sender<(&'static str, Arc<str>)>,
    origins: Origins,
) -> Result<(), String> {
    loop {
        let (stream, peer) = listener.accept().await.map_err(|e| e.to_string())?;
        let webview = webview.clone();
        let events = events.subscribe();
        let origins = origins.clone();
        tauri::async_runtime::spawn(async move {
            eprintln!("PandaCode core: {peer} connected");
            if let Err(error) = client(stream, webview, events, origins).await {
                eprintln!("PandaCode core: {peer}: {error}");
            }
            eprintln!("PandaCode core: {peer} disconnected");
        });
    }
}

async fn client(
    stream: TcpStream,
    webview: Webview<Rt>,
    mut events: broadcast::Receiver<(&'static str, Arc<str>)>,
    origins: Origins,
) -> Result<(), String> {
    use tokio_tungstenite::tungstenite::handshake::server::{ErrorResponse, Request, Response};
    let check = |request: &Request, response: Response| -> Result<Response, ErrorResponse> {
        let origin = request.headers().get("origin").map(|value| value.to_str().unwrap_or("invalid"));
        if origins.allows(origin) {
            return Ok(response);
        }
        let mut refusal = ErrorResponse::new(Some(format!("Origin {} may not use this core. Start it with --allow-origin.", origin.unwrap_or_default())));
        *refusal.status_mut() = tokio_tungstenite::tungstenite::http::StatusCode::FORBIDDEN;
        Err(refusal)
    };
    let socket = tokio_tungstenite::accept_hdr_async(stream, check).await.map_err(|e| e.to_string())?;
    let (mut sink, mut incoming) = socket.split();
    let (replies, mut outgoing) = mpsc::unbounded_channel::<String>();
    let mut subscribed = HashSet::<String>::new();
    let mut ping = tokio::time::interval(Duration::from_secs(30));
    loop {
        tokio::select! {
            message = incoming.next() => match message {
                Some(Ok(Message::Text(text))) => {
                    if let Some(names) = handle_frame(&text, &webview, &replies) {
                        subscribed = names;
                    }
                }
                Some(Ok(Message::Close(_))) | None => return Ok(()),
                Some(Err(error)) => return Err(error.to_string()),
                Some(Ok(_)) => {}
            },
            Some(reply) = outgoing.recv() => {
                sink.send(Message::Text(reply.into())).await.map_err(|e| e.to_string())?;
            }
            event = events.recv() => match event {
                Ok((channel, frame)) => if subscribed.contains(channel) {
                    sink.send(Message::Text(frame.as_ref().into())).await.map_err(|e| e.to_string())?;
                },
                Err(broadcast::error::RecvError::Lagged(missed)) => {
                    let _ = sink.send(Message::Close(None)).await;
                    return Err(format!("fell {missed} events behind; disconnected so it reloads"));
                }
                Err(broadcast::error::RecvError::Closed) => return Ok(()),
            },
            _ = ping.tick() => {
                sink.send(Message::Ping(Default::default())).await.map_err(|e| e.to_string())?;
            }
        }
    }
}

/// Handles one client frame. Returns the new subscription set for a `{subscribe}` frame.
fn handle_frame(text: &str, webview: &Webview<Rt>, replies: &mpsc::UnboundedSender<String>) -> Option<HashSet<String>> {
    let frame: Value = serde_json::from_str(text).ok()?;
    if let Some(names) = frame.get("subscribe").and_then(Value::as_array) {
        return Some(names.iter().filter_map(Value::as_str).map(str::to_owned).collect());
    }
    let id = frame.get("id")?.as_u64()?;
    let Some(command) = frame.get("command").and_then(Value::as_str).map(str::to_owned) else {
        let _ = replies.send(error_frame(id, json!("A call needs a command.")));
        return None;
    };
    let args = match frame.get("args") {
        Some(Value::Object(args)) => Value::Object(args.clone()),
        None | Some(Value::Null) => json!({}),
        Some(_) => {
            let _ = replies.send(error_frame(id, json!("Command arguments must be an object.")));
            return None;
        }
    };
    dispatch(id, command, args, webview.clone(), replies.clone());
    None
}

/// Runs a command through the desktop handler; the reply goes out when it finishes.
pub(crate) fn dispatch(id: u64, command: String, args: Value, webview: Webview<Rt>, replies: mpsc::UnboundedSender<String>) {
    if unavailable(&command) {
        let _ = replies.send(error_frame(id, json!(format!("{command} is unavailable in headless mode."))));
        return;
    }
    if let Some(result) = local_answer(&command) {
        let _ = replies.send(json!({ "id": id, "ok": true, "result": result }).to_string());
        return;
    }
    let request = InvokeRequest {
        cmd: command,
        callback: CallbackFn(0),
        error: CallbackFn(1),
        url: "tauri://localhost".parse().expect("static URL"),
        body: InvokeBody::Json(args),
        headers: Default::default(),
        invoke_key: tauri::test::INVOKE_KEY.to_string(),
    };
    // Synchronous commands run on the calling thread; keep them off the async workers.
    tauri::async_runtime::spawn_blocking(move || {
        webview.on_message(
            request,
            Box::new(move |_webview, _command, response, _callback, _error| {
                let frame = match response {
                    InvokeResponse::Ok(InvokeResponseBody::Json(json)) => {
                        format!(r#"{{"id":{id},"ok":true,"result":{}}}"#, payload(&json))
                    }
                    InvokeResponse::Ok(InvokeResponseBody::Raw(_)) => {
                        error_frame(id, json!("This command returns binary data, which remote mode does not carry."))
                    }
                    InvokeResponse::Err(error) => error_frame(id, error.0),
                };
                let _ = replies.send(frame);
            }),
        );
    });
}

fn error_frame(id: u64, error: Value) -> String {
    json!({ "id": id, "ok": false, "error": error }).to_string()
}

async fn terminated() {
    #[cfg(unix)]
    {
        let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()).expect("SIGTERM handler");
        tokio::select! {
            _ = tokio::signal::ctrl_c() => {}
            _ = term.recv() => {}
        }
    }
    #[cfg(not(unix))]
    let _ = tokio::signal::ctrl_c().await;
}

#[cfg(test)]
mod tests {
    use super::*;
    use tauri::Emitter;
    use tokio_tungstenite::{connect_async, MaybeTlsStream, WebSocketStream};

    type Client = WebSocketStream<MaybeTlsStream<TcpStream>>;

    /// Sends a call and returns its reply, keeping events that arrive meanwhile.
    async fn call(client: &mut Client, events: &mut Vec<Value>, id: u64, command: &str, args: Value) -> Value {
        client.send(Message::Text(json!({ "id": id, "command": command, "args": args }).to_string().into())).await.unwrap();
        loop {
            let frame = next(client).await;
            if frame.get("event").is_some() { events.push(frame); } else if frame["id"] == id { return frame; }
        }
    }
    async fn next(client: &mut Client) -> Value {
        loop {
            match tokio::time::timeout(Duration::from_secs(10), client.next()).await.expect("core answered").unwrap().unwrap() {
                Message::Text(text) => return serde_json::from_str(&text).unwrap(),
                _ => continue,
            }
        }
    }
    async fn until_output(client: &mut Client, events: &mut Vec<Value>, terminal: &str, needle: &str) -> u64 {
        let mut seen = String::new();
        loop {
            let frame = next(client).await;
            if frame["event"] == "terminal-event" && frame["payload"]["terminalId"] == terminal {
                seen.push_str(frame["payload"]["data"].as_str().unwrap_or_default());
                if seen.contains(needle) { return frame["payload"]["end"].as_u64().unwrap(); }
            }
            events.push(frame);
        }
    }

    #[test]
    fn websocket_clients_call_commands_stream_events_and_reattach_to_terminals() {
        let dir = std::env::temp_dir().join(format!("pandacode-core-test-{}", uuid::Uuid::new_v4()));
        let project = dir.join("project");
        std::fs::create_dir_all(&project).unwrap();
        let Core { app, webview, events, .. } = start(Some(dir.join("data"))).unwrap();
        let handle = app.handle().clone();
        let emitter = handle.clone();
        tauri::async_runtime::block_on(async move {
            let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
            let url = format!("ws://{}", listener.local_addr().unwrap());
            tauri::async_runtime::spawn(accept(listener, webview, events, Origins::new(vec!["http://laptop:1420".into()])));
            // Browsers always send an Origin: a page elsewhere may not drive the core.
            let mut foreign = tokio_tungstenite::tungstenite::client::IntoClientRequest::into_client_request(url.as_str()).unwrap();
            foreign.headers_mut().insert("origin", "https://evil.example".parse().unwrap());
            assert!(connect_async(foreign).await.is_err());
            for allowed in ["http://127.0.0.1:1420", "http://localhost:5173", "http://laptop:1420"] {
                let mut request = tokio_tungstenite::tungstenite::client::IntoClientRequest::into_client_request(url.as_str()).unwrap();
                request.headers_mut().insert("origin", allowed.parse().unwrap());
                assert!(connect_async(request).await.is_ok(), "{allowed}");
            }
            let (mut client, _) = connect_async(&url).await.unwrap();
            let mut seen = Vec::new();

            let info = call(&mut client, &mut seen, 1, "core_info", json!({})).await;
            assert_eq!(info["ok"], true);
            assert_eq!(info["result"]["remote"], true);
            // Arguments arrive in the frontend's camelCase, exactly as over Tauri IPC.
            let added = call(&mut client, &mut seen, 2, "add_project", json!({ "path": project.display().to_string() })).await;
            assert_eq!(added["ok"], true, "{added}");
            let project_id = added["result"]["id"].as_str().unwrap().to_owned();
            let missing = call(&mut client, &mut seen, 3, "get_task", json!({})).await;
            assert_eq!(missing["ok"], false);
            assert!(missing["error"].as_str().unwrap().contains("sessionId"), "{missing}");
            let unknown = call(&mut client, &mut seen, 4, "no_such_command", json!({})).await;
            assert_eq!(unknown["ok"], false);
            let browser = call(&mut client, &mut seen, 5, "browser_open", json!({ "url": "https://example.com" })).await;
            assert_eq!(browser["error"], "browser_open is unavailable in headless mode.");
            let appearance = call(&mut client, &mut seen, 6, "configure_window_appearance", json!({})).await;
            assert_eq!(appearance["result"]["material"], "solid");

            // Only subscribed events are forwarded.
            client.send(Message::Text(json!({ "subscribe": ["sessions-changed", "terminal-event"] }).to_string().into())).await.unwrap();
            call(&mut client, &mut seen, 7, "core_info", json!({})).await; // the subscription is in place
            emitter.emit("vm-changed", ()).unwrap();
            emitter.emit("sessions-changed", ()).unwrap();
            let frame = next(&mut client).await;
            assert_eq!(frame, json!({ "event": "sessions-changed", "payload": null }));

            // A terminal streams both ways.
            let started = call(&mut client, &mut seen, 8, "start_terminal", json!({ "projectId": project_id, "cols": 80, "rows": 24 })).await;
            let terminal = started["result"].as_str().expect("terminal id").to_owned();
            call(&mut client, &mut seen, 9, "write_terminal", json!({ "terminalId": terminal, "data": "echo first-$((40+2))\n" })).await;
            let end = until_output(&mut client, &mut seen, &terminal, "first-42").await;

            // The client goes away; the shell keeps running and a new client finds it.
            client.close(None).await.unwrap();
            drop(client);
            let (mut client, _) = connect_async(&url).await.unwrap();
            let mut seen = Vec::new();
            let live = call(&mut client, &mut seen, 1, "list_terminals", json!({})).await;
            assert_eq!(live["result"], json!([{ "id": terminal, "projectId": project_id, "sessionId": null, "kind": "shell" }]));
            let snapshot = call(&mut client, &mut seen, 2, "terminal_snapshot", json!({ "terminalId": terminal })).await;
            assert!(snapshot["result"]["data"].as_str().unwrap().contains("first-42"));
            assert!(snapshot["result"]["end"].as_u64().unwrap() >= end);
            client.send(Message::Text(json!({ "subscribe": ["terminal-event"] }).to_string().into())).await.unwrap();
            call(&mut client, &mut seen, 3, "write_terminal", json!({ "terminalId": terminal, "data": "echo second-$((40+3))\n" })).await;
            let later = until_output(&mut client, &mut seen, &terminal, "second-43").await;
            assert!(later > snapshot["result"]["end"].as_u64().unwrap());
            let closed = call(&mut client, &mut seen, 4, "close_terminal", json!({ "terminalId": terminal })).await;
            assert_eq!(closed["ok"], true);
        });
        crate::begin_shutdown(&handle);
        drop(app);
        let _ = std::fs::remove_dir_all(dir);
    }

    /// Every channel the Rust side emits or the frontend listens to is forwarded,
    /// except the embedded browser's and the desktop launcher's.
    #[test]
    fn channels_cover_every_emit() {
        const DESKTOP_ONLY: &[&str] = &["browser-state", "browser-element", "overlay-visibility", "overlay-open-task"];
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
        let mut emitted = std::collections::BTreeSet::new();
        let emit = regex_lite(r#".emit("#);
        let emit_to = regex_lite(r#".emit_to("#);
        let mut stack = vec![root.join("src")];
        while let Some(path) = stack.pop() {
            for entry in std::fs::read_dir(&path).unwrap() {
                let path = entry.unwrap().path();
                if path.is_dir() { stack.push(path); continue; }
                if path.extension().is_none_or(|e| e != "rs") || path.ends_with("remote_core.rs") { continue; }
                let text = std::fs::read_to_string(&path).unwrap();
                for (marker, skip) in [(&emit, 0), (&emit_to, 1)] {
                    for start in marker(&text) {
                        let literals = string_literals(&text[start..]);
                        let channel = literals.get(skip).unwrap_or_else(|| panic!("{} emits a non-literal channel", path.display()));
                        emitted.insert(channel.clone());
                    }
                }
            }
        }
        let frontend = std::fs::read_dir(root.join("../src")).unwrap();
        for entry in frontend {
            let path = entry.unwrap().path();
            if path.extension().is_none_or(|e| e != "ts") { continue; }
            let text = std::fs::read_to_string(&path).unwrap();
            for marker in ["subscribe('", "listen('", "listen<"] {
                for (index, _) in text.match_indices(marker) {
                    if let Some(name) = text[index..].split('\'').nth(1).filter(|n| n.contains('-') && n.chars().all(|c| c.is_ascii_lowercase() || c == '-')) {
                        emitted.insert(name.to_owned());
                    }
                }
            }
        }
        let missing: Vec<_> = emitted.iter().filter(|c| !CHANNELS.contains(&c.as_str()) && !DESKTOP_ONLY.contains(&c.as_str())).collect();
        assert!(missing.is_empty(), "add these to CHANNELS: {missing:?}");
        assert!(emitted.len() >= CHANNELS.len());
    }

    /// Offsets just past each occurrence of `needle` (a fixed string).
    fn regex_lite(needle: &'static str) -> impl Fn(&str) -> Vec<usize> {
        move |text| text.match_indices(needle).map(|(i, m)| i + m.len()).collect()
    }
    /// The first two string literals of a call, skipping whitespace and line breaks.
    fn string_literals(text: &str) -> Vec<String> {
        let call = &text[..text.find(')').unwrap_or(text.len()).min(400)];
        call.split('"').skip(1).step_by(2).take(2).map(str::to_owned).collect()
    }
}
