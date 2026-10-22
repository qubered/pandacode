//! The orchestrator's HTTP MCP endpoint (unit A). Contract only; the server is not built yet.
use crate::{AppHandle, Result};

/// A running endpoint; `stop` closes its listener.
pub struct Handle;
impl Handle {
    pub fn stop(self) {}
}

/// Serves `POST /mcp` on `address` until stopped.
pub fn start(app: AppHandle, address: std::net::SocketAddr) -> Result<Handle> {
    let _ = (app, address);
    Ok(Handle)
}
