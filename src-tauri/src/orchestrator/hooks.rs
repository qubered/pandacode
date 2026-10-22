//! Signed webhooks to the orchestrator (unit C). Contract only; the sender is not built yet.
use crate::AppHandle;

/// What happened to a supervised task. Serialized as `event_type` `task.<snake_case>`.
#[derive(Clone, Debug)]
pub enum Event {
    Finished { outcome: String, final_reply: String, changed_files: Vec<String> },
    NeedsResponse { kind: String, request_id: String, text: String },
    HumanMessage { text: String, delivery: String },
    HumanResponded { request_id: String, approved: Option<bool>, answer: Option<String> },
    TakenOver,
    HandedOver { last_reply: String },
    Released,
}

/// Starts the delivery worker. Called once from `orchestrator::setup`.
pub fn start(_app: &AppHandle) {}

/// Queues `event` for a task, if the task has a supervisor and a webhook is configured.
pub fn notify(app: &AppHandle, session_id: &str, event: Event) {
    let _ = (app, session_id, event);
}

/// Like `notify`, for a task snapshot taken before its supervisor changed.
pub fn notify_with(app: &AppHandle, task: &crate::storage::Session, event: Event) {
    let _ = (app, task, event);
}
