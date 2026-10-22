//! Signed webhooks to the orchestrator (docs/superpowers/specs/2026-10-22-hermes-orchestrator-design.md,
//! section 3). Events on tasks that have a supervisor are POSTed to `webhook.url` in Hermes's
//! "Generic V2" format. A single worker sends them in order from an in-memory queue and retries
//! failures on a schedule, without holding up the events behind a failing one.
use crate::AppHandle;
use hmac::{Hmac, Mac};
use serde_json::{json, Value};
use sha2::Sha256;
use std::collections::VecDeque;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;
use tauri::Manager;
use tokio::sync::Notify;
use tokio::time::Instant;

/// What happened to a supervised task. Serialized as `event_type` `task.<snake_case>`.
#[derive(Clone, Debug)]
pub enum Event {
    /// `outcome` is `done`, `failed`, `stopped` or `limited`. An empty `final_reply` is read from the
    /// task's latest assistant message, and empty `changed_files` from the latest turn's change summary.
    Finished { outcome: String, final_reply: String, changed_files: Vec<String> },
    NeedsResponse { kind: String, request_id: String, text: String },
    HumanMessage { text: String, delivery: String },
    HumanResponded { request_id: String, approved: Option<bool>, answer: Option<String> },
    TakenOver,
    /// An empty `last_reply` is read from the task's latest assistant message.
    HandedOver { last_reply: String },
    /// Sent by the orchestrator's `release_task` tool.
    #[allow(dead_code)]
    Released,
}

/// Text fields in `data` are cut to this many bytes, on a character boundary.
const TEXT_CAP: usize = 2048;
/// Events kept while the receiver is slow or down; the oldest are dropped first.
const QUEUE_CAP: usize = 1000;
const REQUEST_TIMEOUT: Duration = Duration::from_secs(10);
/// Waits before the 2nd to 6th attempts; after the last one the event is dropped.
const RETRY_DELAYS: [Duration; 5] = [
    Duration::from_secs(2),
    Duration::from_secs(10),
    Duration::from_secs(60),
    Duration::from_secs(5 * 60),
    Duration::from_secs(30 * 60),
];

static OUTBOX: OnceLock<Arc<Outbox>> = OnceLock::new();

/// Starts the delivery worker. Called once from `orchestrator::setup`.
pub fn start(_app: &AppHandle) {
    if OUTBOX.get().is_some() {
        return;
    }
    let client = match http_client() {
        Ok(client) => client,
        Err(error) => {
            eprintln!("[orchestrator] webhooks are off: could not build the HTTP client: {error}");
            return;
        }
    };
    let outbox = Arc::new(Outbox::new(QUEUE_CAP));
    if OUTBOX.set(outbox.clone()).is_err() {
        return;
    }
    let engine = Engine {
        client,
        secret: Arc::new(|| super::webhook_secret().filter(|secret| !secret.is_empty())),
        delays: RETRY_DELAYS.to_vec(),
    };
    tauri::async_runtime::spawn(outbox.run(engine));
}

/// Queues `event` for a task, if the task has a supervisor and a webhook is configured.
pub fn notify(app: &AppHandle, session_id: &str, event: Event) {
    let Some(outbox) = OUTBOX.get() else { return };
    let Some(state) = app.try_state::<crate::AppState>() else { return };
    let task = match state.store.lock() {
        Ok(store) => store.session(session_id).ok(),
        Err(_) => None,
    };
    if let Some(task) = task {
        enqueue(app, outbox, &task, event);
    }
}

/// Like `notify`, for a task snapshot taken before its supervisor changed.
pub fn notify_with(app: &AppHandle, task: &crate::storage::Session, event: Event) {
    if let Some(outbox) = OUTBOX.get() {
        enqueue(app, outbox, task, event);
    }
}

/// An approval or a question arrived from the agent. `e.request_id` is the scoped ID the UI uses.
pub fn needs_response(app: &AppHandle, e: &crate::providers::AgentEvent) {
    let Some(request_id) = e.request_id.clone() else { return };
    let (kind, text) = match e.kind.as_str() {
        "approval" => ("approval", e.text.clone()),
        "input_request" => ("question", e.data.as_ref().map(question_text).unwrap_or_default()),
        _ => return,
    };
    notify(app, &e.session_id, Event::NeedsResponse { kind: kind.into(), request_id, text });
}

/// The question a structured input request (`kind` questions, form or url) asks.
fn question_text(data: &Value) -> String {
    match data["kind"].as_str() {
        Some("questions") => data["questions"]
            .as_array()
            .map(|questions| questions.iter().filter_map(|q| q["question"].as_str()).collect::<Vec<_>>().join("\n"))
            .unwrap_or_default(),
        _ => data["message"].as_str().unwrap_or_default().to_owned(),
    }
}

fn enqueue(app: &AppHandle, outbox: &Outbox, task: &crate::storage::Session, event: Event) {
    // A released task has no supervisor any more, but the orchestrator still hears about it.
    if task.supervisor.is_none() && !matches!(event, Event::Released) {
        return;
    }
    let url = match super::settings(app).webhook {
        Some(webhook) if !webhook.url.trim().is_empty() => webhook.url,
        _ => return,
    };
    let mut event = event;
    let mut changed_from_store = None;
    let mut outcome = None;
    let project_name;
    {
        let Some(state) = app.try_state::<crate::AppState>() else { return };
        let Ok(store) = state.store.lock() else { return };
        project_name = store.project(&task.project_id).map(|p| p.name).unwrap_or_default();
        match &mut event {
            Event::Finished { final_reply, changed_files, outcome: kind } => {
                if final_reply.is_empty() {
                    *final_reply = last_reply(&store, &task.id, true);
                }
                if changed_files.is_empty() {
                    changed_from_store = Some(changed_files_of_last_turn(&store, &task.id));
                }
                // The run ended on the account's usage limit rather than an ordinary failure.
                if kind == "failed" && task.limited_at.is_some() {
                    outcome = Some("limited");
                }
            }
            Event::HandedOver { last_reply: reply } if reply.is_empty() => *reply = last_reply(&store, &task.id, false),
            _ => {}
        }
    }
    if let Event::Finished { changed_files, outcome: kind, .. } = &mut event {
        if let Some(files) = changed_from_store {
            *changed_files = files;
        }
        if let Some(limited) = outcome {
            *kind = limited.into();
        }
    }
    let (id, body) = build_body(&task_info(task, project_name), &event);
    outbox.push(Job { id, event_type: event_type(&event), url, body, attempt: 0, due: Instant::now() });
}

/// The latest assistant text of the task (of its latest turn only when `current_turn`), cut to the cap.
fn last_reply(store: &crate::storage::Store, sid: &str, current_turn: bool) -> String {
    let after = if current_turn { "AND id>(SELECT COALESCE(MAX(id),0) FROM messages WHERE session_id=?1 AND role IN ('user','coordination'))" } else { "" };
    let sql = format!("SELECT CAST(substr(CAST(content AS BLOB),1,{}) AS BLOB) FROM messages WHERE session_id=?1 AND role='assistant' AND content<>'' {after} ORDER BY id DESC LIMIT 1", TEXT_CAP + 8);
    store
        .db
        .query_row(&sql, [sid], |row| row.get::<_, Vec<u8>>(0))
        .ok()
        .map(|bytes| cap_text(&String::from_utf8_lossy(&bytes), TEXT_CAP))
        .unwrap_or_default()
}

/// Paths changed by the task's latest turn, from the summary saved when the turn ended.
fn changed_files_of_last_turn(store: &crate::storage::Store, sid: &str) -> Vec<String> {
    let summary: Option<String> = store
        .db
        .query_row(
            "SELECT summary FROM turn_changes WHERE session_id=?1 AND message_id>(SELECT COALESCE(MAX(id),0) FROM messages WHERE session_id=?1 AND role IN ('user','coordination')) ORDER BY message_id DESC LIMIT 1",
            [sid],
            |row| row.get(0),
        )
        .ok();
    summary.and_then(|s| serde_json::from_str::<Value>(&s).ok()).map(|v| changed_paths(&v)).unwrap_or_default()
}

fn changed_paths(summary: &Value) -> Vec<String> {
    summary["files"]
        .as_array()
        .map(|files| files.iter().filter_map(|f| f["path"].as_str()).take(100).map(|p| cap_text(p, 512)).collect())
        .unwrap_or_default()
}

// ---- payload ----------------------------------------------------------------------------------

/// The `task` object of a payload.
#[derive(Clone, Debug, PartialEq)]
struct TaskInfo {
    id: String,
    project_id: String,
    project_name: String,
    title: String,
    status: String,
    supervisor: Option<String>,
    provider: String,
    model: String,
}

fn task_info(task: &crate::storage::Session, project_name: String) -> TaskInfo {
    TaskInfo {
        id: task.id.clone(),
        project_id: task.project_id.clone(),
        project_name,
        title: task.title.clone(),
        status: task.status.clone(),
        supervisor: task.supervisor.clone(),
        provider: task.provider.clone(),
        model: task.model.clone(),
    }
}

fn event_type(event: &Event) -> &'static str {
    match event {
        Event::Finished { .. } => "task.finished",
        Event::NeedsResponse { .. } => "task.needs_response",
        Event::HumanMessage { .. } => "task.human_message",
        Event::HumanResponded { .. } => "task.human_responded",
        Event::TakenOver => "task.taken_over",
        Event::HandedOver { .. } => "task.handed_over",
        Event::Released => "task.released",
    }
}

/// Cuts `text` to at most `max` bytes without splitting a character.
fn cap_text(text: &str, max: usize) -> String {
    if text.len() <= max {
        return text.to_owned();
    }
    let mut end = max;
    while !text.is_char_boundary(end) {
        end -= 1;
    }
    text[..end].to_owned()
}

/// `text` as a single line of at most `max` characters, for `summary`.
fn one_line(text: &str, max: usize) -> String {
    let flat = text.split_whitespace().collect::<Vec<_>>().join(" ");
    if flat.chars().count() <= max {
        return flat;
    }
    let mut cut: String = flat.chars().take(max.saturating_sub(1)).collect();
    cut.push('…');
    cut
}

fn data_and_summary(task: &TaskInfo, event: &Event) -> (Value, String) {
    let named = format!("\"{}\" in {}", one_line(&task.title, 80), one_line(&task.project_name, 60));
    let what = format!("Task {named}");
    let lower = format!("task {named}");
    match event {
        Event::Finished { outcome, final_reply, changed_files } => {
            let verdict = match outcome.as_str() {
                "done" => "finished",
                "failed" => "failed",
                "stopped" => "was stopped",
                "limited" => "stopped at a usage limit",
                other => other,
            };
            let reply = one_line(final_reply, 160);
            let summary = if reply.is_empty() { format!("{what} {verdict}.") } else { format!("{what} {verdict}. Last reply: {reply}") };
            let data = json!({"outcome": outcome, "finalReply": cap_text(final_reply, TEXT_CAP), "changedFiles": changed_files});
            (data, summary)
        }
        Event::NeedsResponse { kind, request_id, text } => {
            let ask = if kind == "approval" { "needs an approval" } else { "has a question" };
            let data = json!({"kind": kind, "requestId": request_id, "text": cap_text(text, TEXT_CAP)});
            (data, format!("{what} {ask}: {}", one_line(text, 160)))
        }
        Event::HumanMessage { text, delivery } => {
            let how = match delivery.as_str() {
                "queue" => "queued a message for",
                "steer" => "steered",
                _ => "sent a message to",
            };
            let data = json!({"text": cap_text(text, TEXT_CAP), "delivery": delivery});
            (data, format!("The user {how} {}: {}", lower, one_line(text, 160)))
        }
        Event::HumanResponded { request_id, approved, answer } => {
            let verb = match approved {
                Some(true) => "approved a request on",
                Some(false) => "denied a request on",
                None => "answered a question on",
            };
            let mut data = json!({"requestId": request_id});
            if let Some(approved) = approved {
                data["approved"] = json!(approved);
            }
            if let Some(answer) = answer {
                data["answer"] = json!(cap_text(answer, TEXT_CAP));
            }
            (data, format!("The user {verb} {}.", lower))
        }
        Event::TakenOver => (json!({}), format!("The user took over {}.", lower)),
        Event::HandedOver { last_reply } => {
            let data = json!({"lastReply": cap_text(last_reply, TEXT_CAP)});
            (data, format!("The user handed {} to you.", lower))
        }
        Event::Released => (json!({}), format!("{what} was released.")),
    }
}

/// A fresh event: its id and the exact JSON bytes to sign and send.
fn build_body(task: &TaskInfo, event: &Event) -> (String, Vec<u8>) {
    let id = uuid::Uuid::new_v4().to_string();
    let body = payload(&id, crate::storage::now(), task, event);
    (id, serde_json::to_vec(&body).unwrap_or_default())
}

fn payload(id: &str, occurred_at: i64, task: &TaskInfo, event: &Event) -> Value {
    let (data, summary) = data_and_summary(task, event);
    json!({
        "event_type": event_type(event),
        "id": id,
        "occurredAt": occurred_at,
        "task": {
            "id": task.id,
            "projectId": task.project_id,
            "projectName": task.project_name,
            "title": task.title,
            "status": task.status,
            "supervisor": task.supervisor,
            "provider": task.provider,
            "model": task.model,
        },
        "summary": summary,
        "data": data,
    })
}

// ---- signing ----------------------------------------------------------------------------------

/// Hermes "Generic V2": hex HMAC-SHA256 of `"<timestamp>.<body>"` keyed by the shared secret.
fn sign(secret: &str, timestamp: u64, body: &[u8]) -> String {
    let mut mac = Hmac::<Sha256>::new_from_slice(secret.as_bytes()).expect("HMAC accepts any key length");
    mac.update(timestamp.to_string().as_bytes());
    mac.update(b".");
    mac.update(body);
    mac.finalize().into_bytes().iter().map(|byte| format!("{byte:02x}")).collect()
}

// ---- delivery ---------------------------------------------------------------------------------

/// How long to wait before attempt `attempt + 2`, or `None` to give up. `status` is the response
/// code of attempt number `attempt` (counting from 0); `None` means the request itself failed.
/// A 2xx is delivered, so it never retries; a 4xx other than 429 will not change on retry.
fn retry_delay(status: Option<u16>, attempt: usize, delays: &[Duration]) -> Option<Duration> {
    let retryable = match status {
        None => true,
        Some(code) => code == 429 || code >= 500,
    };
    retryable.then(|| delays.get(attempt).copied()).flatten()
}

struct Job {
    id: String,
    event_type: &'static str,
    url: String,
    body: Vec<u8>,
    /// Attempts already made.
    attempt: usize,
    /// Not sent before this time.
    due: Instant,
}

/// In-memory FIFO of events waiting to be sent, or to be retried.
struct Outbox {
    jobs: Mutex<VecDeque<Job>>,
    cap: usize,
    wake: Notify,
}

enum Take {
    Job(Job),
    /// Nothing is due; the soonest retry is at this time, if any is waiting.
    Wait(Option<Instant>),
}

impl Outbox {
    fn new(cap: usize) -> Self {
        Self { jobs: Mutex::new(VecDeque::new()), cap, wake: Notify::new() }
    }

    fn push(&self, job: Job) {
        if let Ok(mut jobs) = self.jobs.lock() {
            while jobs.len() >= self.cap {
                if let Some(old) = jobs.pop_front() {
                    eprintln!("[orchestrator] webhook queue full: dropped {} {}", old.event_type, old.id);
                }
            }
            jobs.push_back(job);
        }
        self.wake.notify_one();
    }

    #[cfg(test)]
    fn len(&self) -> usize {
        self.jobs.lock().map(|jobs| jobs.len()).unwrap_or(0)
    }

    /// The oldest job that is due. Jobs waiting to retry keep their place but are passed over.
    fn take(&self, now: Instant) -> Take {
        let Ok(mut jobs) = self.jobs.lock() else { return Take::Wait(None) };
        match jobs.iter().position(|job| job.due <= now) {
            Some(index) => Take::Job(jobs.remove(index).expect("index from position")),
            None => Take::Wait(jobs.iter().map(|job| job.due).min()),
        }
    }

    async fn run(self: Arc<Self>, engine: Engine) {
        loop {
            match self.take(Instant::now()) {
                Take::Job(job) => {
                    if let Some(retry) = engine.deliver(job).await {
                        // Back in its old place would let it block newer events; the back keeps order by due time.
                        self.requeue(retry);
                    }
                }
                Take::Wait(Some(until)) => {
                    tokio::select! {
                        _ = self.wake.notified() => {}
                        _ = tokio::time::sleep_until(until) => {}
                    }
                }
                Take::Wait(None) => self.wake.notified().await,
            }
        }
    }

    fn requeue(&self, job: Job) {
        if let Ok(mut jobs) = self.jobs.lock() {
            jobs.push_back(job);
        }
    }
}

struct Engine {
    client: reqwest::Client,
    /// The webhook secret, read for every attempt so a changed secret applies at once.
    secret: Arc<dyn Fn() -> Option<String> + Send + Sync>,
    delays: Vec<Duration>,
}

impl Engine {
    /// Sends `job` once. Returns it, due later, when it should be retried.
    async fn deliver(&self, mut job: Job) -> Option<Job> {
        let secret = self.secret.clone();
        let Some(secret) = tokio::task::spawn_blocking(move || secret()).await.ok().flatten() else {
            // The secret was removed after the event was queued.
            return None;
        };
        let timestamp = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap_or_default().as_secs();
        let signature = sign(&secret, timestamp, &job.body);
        let sent = self
            .client
            .post(&job.url)
            .header(reqwest::header::CONTENT_TYPE, "application/json")
            .header("X-Webhook-Timestamp", timestamp.to_string())
            .header("X-Webhook-Signature-V2", signature)
            .header("X-Request-ID", &job.id)
            .body(job.body.clone())
            .send()
            .await;
        let status = match &sent {
            Ok(response) if response.status().is_success() => return None,
            Ok(response) => Some(response.status().as_u16()),
            Err(_) => None,
        };
        match retry_delay(status, job.attempt, &self.delays) {
            Some(delay) => {
                job.attempt += 1;
                job.due = Instant::now() + delay;
                Some(job)
            }
            None => {
                let reason = status.map_or("no response".to_owned(), |code| format!("status {code}"));
                eprintln!("[orchestrator] webhook {} {} dropped after {} attempt(s): {reason}", job.event_type, job.id, job.attempt + 1);
                None
            }
        }
    }
}

fn http_client() -> Result<reqwest::Client, String> {
    if rustls::crypto::CryptoProvider::get_default().is_none() {
        let _ = rustls::crypto::ring::default_provider().install_default();
    }
    reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .connect_timeout(Duration::from_secs(5))
        .timeout(REQUEST_TIMEOUT)
        .user_agent(concat!("PandaCode/", env!("CARGO_PKG_VERSION")))
        .build()
        .map_err(|e| e.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;

    fn task() -> TaskInfo {
        TaskInfo {
            id: "t1".into(),
            project_id: "p1".into(),
            project_name: "Panda".into(),
            title: "Fix the login bug".into(),
            status: "running".into(),
            supervisor: Some("hermes".into()),
            provider: "claude".into(),
            model: "opus".into(),
        }
    }

    #[test]
    fn signature_matches_an_independent_hmac() {
        // printf '1700000000.{"a":1}' | openssl dgst -sha256 -hmac s3cret
        let signature = sign("s3cret", 1_700_000_000, br#"{"a":1}"#);
        assert_eq!(signature, "1698a50bc74d1ff1db85c4e0a5297c2ad9fdba245d5737cdb789e4cc6e098940");
        assert_eq!(signature.len(), 64);
        assert_ne!(signature, sign("s3cret", 1_700_000_001, br#"{"a":1}"#));
        assert_ne!(signature, sign("other", 1_700_000_000, br#"{"a":1}"#));
    }

    #[test]
    fn payload_shape_for_every_event() {
        let cases: Vec<(Event, &str, Value)> = vec![
            (
                Event::Finished { outcome: "done".into(), final_reply: "All fixed.\nTests pass.".into(), changed_files: vec!["src/a.rs".into()] },
                "task.finished",
                json!({"outcome": "done", "finalReply": "All fixed.\nTests pass.", "changedFiles": ["src/a.rs"]}),
            ),
            (
                Event::NeedsResponse { kind: "approval".into(), request_id: "run:7".into(), text: "Run cargo test?".into() },
                "task.needs_response",
                json!({"kind": "approval", "requestId": "run:7", "text": "Run cargo test?"}),
            ),
            (
                Event::HumanMessage { text: "use tabs".into(), delivery: "steer".into() },
                "task.human_message",
                json!({"text": "use tabs", "delivery": "steer"}),
            ),
            (
                Event::HumanResponded { request_id: "run:7".into(), approved: Some(false), answer: None },
                "task.human_responded",
                json!({"requestId": "run:7", "approved": false}),
            ),
            (
                Event::HumanResponded { request_id: "run:8".into(), approved: None, answer: Some("{\"a\":\"b\"}".into()) },
                "task.human_responded",
                json!({"requestId": "run:8", "answer": "{\"a\":\"b\"}"}),
            ),
            (Event::TakenOver, "task.taken_over", json!({})),
            (Event::HandedOver { last_reply: "Done so far.".into() }, "task.handed_over", json!({"lastReply": "Done so far."})),
            (Event::Released, "task.released", json!({})),
        ];
        for (event, kind, data) in cases {
            let body = payload("0d9f6a52-4a1e-4c7b-8f4b-2f3c9b2a1e10", 1_792_627_000_000, &task(), &event);
            assert_eq!(body["event_type"], kind);
            assert_eq!(body["id"], "0d9f6a52-4a1e-4c7b-8f4b-2f3c9b2a1e10");
            assert_eq!(body["occurredAt"], 1_792_627_000_000_i64);
            assert_eq!(
                body["task"],
                json!({"id": "t1", "projectId": "p1", "projectName": "Panda", "title": "Fix the login bug", "status": "running", "supervisor": "hermes", "provider": "claude", "model": "opus"})
            );
            assert_eq!(body["data"], data, "{kind}");
            let summary = body["summary"].as_str().unwrap();
            assert!(!summary.is_empty() && !summary.contains('\n'), "{kind}: {summary:?}");
            assert!(summary.contains("Fix the login bug") && summary.contains("Panda"), "{summary}");
            assert_eq!(body.as_object().unwrap().len(), 6);
        }
    }

    #[test]
    fn summaries_are_one_line_and_bounded() {
        let long = format!("first line\n{}", "x".repeat(5000));
        let event = Event::Finished { outcome: "failed".into(), final_reply: long, changed_files: vec![] };
        let body = payload("id", 1, &task(), &event);
        let summary = body["summary"].as_str().unwrap();
        assert!(!summary.contains('\n') && summary.chars().count() < 400);
        assert!(summary.contains("failed"));
        assert_eq!(body["data"]["finalReply"].as_str().unwrap().len(), TEXT_CAP);
    }

    #[test]
    fn text_is_cut_on_character_boundaries() {
        assert_eq!(cap_text("short", TEXT_CAP), "short");
        let ascii = "a".repeat(5000);
        assert_eq!(cap_text(&ascii, TEXT_CAP).len(), TEXT_CAP);
        // 2048 is odd-aligned for 3-byte characters (2048 = 3 * 682 + 2), so the cut backs off by two bytes.
        let wide = "日".repeat(1000);
        let cut = cap_text(&wide, TEXT_CAP);
        assert_eq!(cut.len(), 2046);
        assert!(wide.starts_with(&cut));
        // A 2-byte character straddling the limit.
        let text = format!("{}é{}", "a".repeat(TEXT_CAP - 1), "b".repeat(10));
        let cut = cap_text(&text, TEXT_CAP);
        assert_eq!(cut.len(), TEXT_CAP - 1);
        let body = payload("id", 1, &task(), &Event::HumanMessage { text: wide, delivery: "message".into() });
        assert!(body["data"]["text"].as_str().unwrap().len() <= TEXT_CAP);
    }

    #[test]
    fn questions_become_text() {
        let data = json!({"kind": "questions", "questions": [{"question": "Which approach?"}, {"question": "Keep tests?"}]});
        assert_eq!(question_text(&data), "Which approach?\nKeep tests?");
        assert_eq!(question_text(&json!({"kind": "form", "message": "Configure the task"})), "Configure the task");
    }

    #[test]
    fn changed_paths_come_from_the_summary() {
        let summary = json!({"id": "c", "files": [{"path": "a.rs", "status": "M"}, {"path": "b/c.ts", "status": "A"}]});
        assert_eq!(changed_paths(&summary), vec!["a.rs".to_owned(), "b/c.ts".to_owned()]);
        assert!(changed_paths(&json!({})).is_empty());
    }

    #[test]
    fn retry_policy() {
        let d = RETRY_DELAYS;
        let secs = |n| Some(Duration::from_secs(n));
        // Network errors, 429 and 5xx back off 2 s, 10 s, 60 s, 5 min, 30 min, then give up.
        for status in [None, Some(429), Some(500), Some(502), Some(503)] {
            assert_eq!(retry_delay(status, 0, &d), secs(2), "{status:?}");
            assert_eq!(retry_delay(status, 1, &d), secs(10));
            assert_eq!(retry_delay(status, 2, &d), secs(60));
            assert_eq!(retry_delay(status, 3, &d), secs(300));
            assert_eq!(retry_delay(status, 4, &d), secs(1800));
            assert_eq!(retry_delay(status, 5, &d), None);
        }
        // Other client errors will not change; success and redirects are not retried.
        for status in [Some(400), Some(401), Some(403), Some(404), Some(410), Some(422), Some(204), Some(302)] {
            assert_eq!(retry_delay(status, 0, &d), None, "{status:?}");
        }
    }

    fn job(id: &str) -> Job {
        Job { id: id.into(), event_type: "task.released", url: "http://127.0.0.1:1/".into(), body: b"{}".to_vec(), attempt: 0, due: Instant::now() }
    }

    #[test]
    fn queue_is_capped_and_drops_the_oldest() {
        let outbox = Outbox::new(QUEUE_CAP);
        for n in 0..QUEUE_CAP + 5 {
            outbox.push(job(&n.to_string()));
        }
        assert_eq!(outbox.len(), QUEUE_CAP);
        let Take::Job(first) = outbox.take(Instant::now()) else { panic!("a job is due") };
        assert_eq!(first.id, "5");
    }

    #[test]
    fn a_waiting_retry_does_not_block_later_events() {
        let outbox = Outbox::new(10);
        let mut waiting = job("old");
        waiting.due = Instant::now() + Duration::from_secs(60);
        outbox.push(waiting);
        outbox.push(job("new"));
        let Take::Job(next) = outbox.take(Instant::now()) else { panic!("a job is due") };
        assert_eq!(next.id, "new");
        assert!(matches!(outbox.take(Instant::now()), Take::Wait(Some(_))));
    }

    struct Received {
        headers: Vec<(String, String)>,
        body: Vec<u8>,
    }
    impl Received {
        fn header(&self, name: &str) -> &str {
            self.headers.iter().find(|(k, _)| k.eq_ignore_ascii_case(name)).map(|(_, v)| v.as_str()).unwrap_or_default()
        }
    }

    /// Answers each request with the next status in `statuses` (the last repeats) and records it.
    async fn receiver(statuses: Vec<u16>) -> (String, Arc<Mutex<Vec<Received>>>) {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}/hook", listener.local_addr().unwrap());
        let seen = Arc::new(Mutex::new(Vec::new()));
        let log = seen.clone();
        tokio::spawn(async move {
            loop {
                let Ok((mut socket, _)) = listener.accept().await else { return };
                let mut data = Vec::new();
                let mut chunk = [0u8; 4096];
                let (head_end, length) = loop {
                    let Ok(n) = socket.read(&mut chunk).await else { break (0, 0) };
                    if n == 0 {
                        break (0, 0);
                    }
                    data.extend_from_slice(&chunk[..n]);
                    if let Some(end) = data.windows(4).position(|w| w == b"\r\n\r\n") {
                        let head = String::from_utf8_lossy(&data[..end]).to_lowercase();
                        let length = head.lines().find_map(|l| l.strip_prefix("content-length:")).and_then(|v| v.trim().parse().ok()).unwrap_or(0usize);
                        break (end + 4, length);
                    }
                };
                if head_end == 0 {
                    continue;
                }
                while data.len() < head_end + length {
                    let Ok(n) = socket.read(&mut chunk).await else { break };
                    if n == 0 {
                        break;
                    }
                    data.extend_from_slice(&chunk[..n]);
                }
                let head = String::from_utf8_lossy(&data[..head_end - 4]).into_owned();
                let headers = head.lines().skip(1).filter_map(|l| l.split_once(':')).map(|(k, v)| (k.trim().to_owned(), v.trim().to_owned())).collect();
                let status = {
                    let mut log = log.lock().unwrap();
                    log.push(Received { headers, body: data[head_end..head_end + length].to_vec() });
                    statuses[(log.len() - 1).min(statuses.len() - 1)]
                };
                let reply = format!("HTTP/1.1 {status} X\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
                let _ = socket.write_all(reply.as_bytes()).await;
                let _ = socket.shutdown().await;
            }
        });
        (url, seen)
    }

    fn engine(delay: Duration) -> Engine {
        Engine { client: http_client().unwrap(), secret: Arc::new(|| Some("s3cret".into())), delays: vec![delay; 3] }
    }

    async fn wait_for(seen: &Arc<Mutex<Vec<Received>>>, count: usize) {
        for _ in 0..200 {
            if seen.lock().unwrap().len() >= count {
                return;
            }
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
        panic!("expected {count} request(s), got {}", seen.lock().unwrap().len());
    }

    #[tokio::test]
    async fn delivers_signed_and_retries_a_500() {
        let (url, seen) = receiver(vec![500, 200]).await;
        let outbox = Arc::new(Outbox::new(10));
        let worker = tokio::spawn(outbox.clone().run(engine(Duration::from_millis(50))));
        let (id, body) = build_body(&task(), &Event::Released);
        outbox.push(Job { id: id.clone(), event_type: "task.released", url, body: body.clone(), attempt: 0, due: Instant::now() });

        wait_for(&seen, 2).await;
        tokio::time::sleep(Duration::from_millis(250)).await;
        {
            let seen = seen.lock().unwrap();
            assert_eq!(seen.len(), 2, "one retry after the 500, none after the 200");
            for request in seen.iter() {
                assert_eq!(request.body, body, "the same bytes are sent each time");
                assert_eq!(request.header("content-type"), "application/json");
                assert_eq!(request.header("x-request-id"), id);
                let timestamp: u64 = request.header("x-webhook-timestamp").parse().expect("unix seconds");
                assert_eq!(request.header("x-webhook-signature-v2"), sign("s3cret", timestamp, &request.body));
            }
            let parsed: Value = serde_json::from_slice(&seen[0].body).unwrap();
            assert_eq!(parsed["event_type"], "task.released");
            assert_eq!(parsed["id"], id);
        }
        assert_eq!(outbox.len(), 0);
        worker.abort();
    }

    #[tokio::test]
    async fn a_400_is_not_retried() {
        let (url, seen) = receiver(vec![400]).await;
        let outbox = Arc::new(Outbox::new(10));
        let worker = tokio::spawn(outbox.clone().run(engine(Duration::from_millis(20))));
        let mut rejected = job("rejected");
        rejected.url = url;
        outbox.push(rejected);
        wait_for(&seen, 1).await;
        tokio::time::sleep(Duration::from_millis(250)).await;
        assert_eq!(seen.lock().unwrap().len(), 1);
        assert_eq!(outbox.len(), 0);
        worker.abort();
    }
}
