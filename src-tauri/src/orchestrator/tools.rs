//! The orchestrator's tools (unit B). Contract only; the handlers are not built yet.
use crate::{AppHandle, Result};
use serde_json::Value;

/// MCP tool definitions (`name`, `description`, `inputSchema`).
pub fn catalog() -> Vec<Value> {
    Vec::new()
}

/// Runs a tool; the result becomes the MCP `tools/call` content.
pub async fn call(app: &AppHandle, name: &str, args: Value) -> Result<Value> {
    let _ = (app, args);
    Err(format!("Unknown tool {name}."))
}

/// At the end of a run on a supervised task, delivers its next queued message natively.
pub fn drain_next(app: &AppHandle, session_id: &str) {
    let _ = (app, session_id);
}
