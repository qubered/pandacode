//! Orchestration by an outside agent such as Hermes
//! (docs/superpowers/specs/2026-10-22-hermes-orchestrator-design.md):
//! - `mcp`: an HTTP MCP endpoint whose tools (`tools`) find, create, message and read tasks.
//! - `hooks`: signed webhooks that wake the orchestrator when a task it supervises needs it.
//! A task's `supervisor` says whether the user or the orchestrator answers its approvals.

pub mod hooks;
pub mod mcp;
pub mod tools;

use crate::{AppHandle, AppState, Result};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use std::sync::{Mutex, RwLock};
use tauri::{Emitter, Manager, State};

/// Secret store service for the MCP token (`token`) and the webhook secret (`webhook-secret`).
pub const SECRET_SERVICE: &str = "app.pandacode.orchestrator";
/// Session modes from least to most autonomous; `maxMode` caps what the orchestrator may launch.
pub const MODES: [&str; 4] = ["plan", "ask", "auto", "full"];

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase", default)]
pub struct Settings {
    pub enabled: bool,
    /// Written to `sessions.supervisor` and `messages.sender`; shown in the UI.
    pub name: String,
    pub bind: String,
    pub port: u16,
    pub max_mode: String,
    pub webhook: Option<Webhook>,
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct Webhook {
    pub url: String,
}
impl Default for Settings {
    fn default() -> Self {
        Self { enabled: false, name: "hermes".into(), bind: "127.0.0.1".into(), port: 7879, max_mode: "auto".into(), webhook: None }
    }
}

/// Managed state: settings, and the running endpoint.
pub struct Orchestrator {
    file: PathBuf,
    settings: RwLock<Settings>,
    server: Mutex<Option<mcp::Handle>>,
}

pub fn settings(app: &AppHandle) -> Settings {
    app.state::<Orchestrator>().settings.read().map(|s| s.clone()).unwrap_or_default()
}

/// The MCP bearer token, created on first use when `create` is set.
pub fn token(create: bool) -> Option<String> {
    if let Some(token) = crate::secret_store::get(SECRET_SERVICE, "token") {
        return Some(token);
    }
    if !create {
        return None;
    }
    let token = format!("{}{}", uuid::Uuid::new_v4().simple(), uuid::Uuid::new_v4().simple());
    crate::secret_store::set(SECRET_SERVICE, "token", &token).ok()?;
    Some(token)
}

pub fn webhook_secret() -> Option<String> {
    crate::secret_store::get(SECRET_SERVICE, "webhook-secret")
}

/// The mode a task may run in: `requested` (or `fallback`) capped at `max`. Returns whether it was capped.
pub fn clamp_mode(requested: Option<&str>, fallback: &str, max: &str) -> (String, bool) {
    let rank = |mode: &str| MODES.iter().position(|m| *m == mode);
    let wanted = requested.filter(|m| rank(m).is_some()).unwrap_or(fallback);
    match (rank(wanted), rank(max)) {
        (Some(w), Some(m)) if w > m => (max.to_owned(), true),
        _ => (wanted.to_owned(), false),
    }
}

/// Loads settings from `orchestrator.json` in `dir`, starts the webhook sender, and
/// starts the endpoint when enabled. Called once from `setup_core`.
pub fn setup(app: &AppHandle, dir: &Path) -> Result<()> {
    let file = dir.join("orchestrator.json");
    let settings = std::fs::read(&file).ok().and_then(|bytes| serde_json::from_slice(&bytes).ok()).unwrap_or_default();
    app.manage(Orchestrator { file, settings: RwLock::new(settings), server: Mutex::new(None) });
    hooks::start(app);
    restart(app)
}

/// Headless flags: enable the endpoint on `bind:port` for this run without saving.
pub fn override_endpoint(app: &AppHandle, bind: &str, port: u16) -> Result<()> {
    if let Ok(mut settings) = app.state::<Orchestrator>().settings.write() {
        settings.enabled = true;
        settings.bind = bind.into();
        settings.port = port;
    }
    restart(app)
}

/// Stops the endpoint and starts it again from the current settings.
fn restart(app: &AppHandle) -> Result<()> {
    let state = app.state::<Orchestrator>();
    let mut server = state.server.lock().map_err(|e| e.to_string())?;
    if let Some(handle) = server.take() {
        handle.stop();
    }
    let settings = settings(app);
    if !settings.enabled {
        return Ok(());
    }
    let ip: std::net::IpAddr = settings.bind.parse().map_err(|_| format!("{} is not an IP address.", settings.bind))?;
    token(true).ok_or("Could not create the orchestrator token.")?;
    *server = Some(mcp::start(app.clone(), (ip, settings.port).into())?);
    Ok(())
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SettingsView {
    #[serde(flatten)]
    settings: Settings,
    mcp_url: String,
    running: bool,
    token_set: bool,
    webhook_secret_set: bool,
}

#[tauri::command]
pub fn orchestrator_settings(app: AppHandle) -> SettingsView {
    let settings = settings(&app);
    let running = app.state::<Orchestrator>().server.lock().map(|s| s.is_some()).unwrap_or(false);
    SettingsView {
        mcp_url: format!("http://{}:{}/mcp", settings.bind, settings.port),
        settings,
        running,
        token_set: token(false).is_some(),
        webhook_secret_set: webhook_secret().is_some(),
    }
}

/// Saves settings (and the webhook secret when given; an empty string removes it) and restarts the endpoint.
#[tauri::command]
pub fn orchestrator_save_settings(app: AppHandle, settings: Settings, webhook_secret: Option<String>) -> Result<SettingsView> {
    let name = settings.name.trim();
    if name.is_empty() || name.len() > 40 || !name.chars().all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_') {
        return Err("Use a short name of letters, numbers, - or _.".into());
    }
    if !MODES.contains(&settings.max_mode.as_str()) {
        return Err("Choose plan, ask, auto or full as the highest mode.".into());
    }
    settings.bind.parse::<std::net::IpAddr>().map_err(|_| "The address must be an IP address.".to_string())?;
    if let Some(webhook) = &settings.webhook {
        let url = tauri::Url::parse(&webhook.url).map_err(|_| "The webhook URL is invalid.".to_string())?;
        if !matches!(url.scheme(), "http" | "https") {
            return Err("The webhook URL must use http or https.".into());
        }
    }
    match webhook_secret.as_deref().map(str::trim) {
        Some("") => crate::secret_store::remove(SECRET_SERVICE, "webhook-secret"),
        Some(secret) => crate::secret_store::set(SECRET_SERVICE, "webhook-secret", secret)?,
        None => {}
    }
    let state = app.state::<Orchestrator>();
    let bytes = serde_json::to_vec_pretty(&settings).map_err(|e| e.to_string())?;
    std::fs::write(&state.file, bytes).map_err(|e| format!("Could not save orchestrator settings: {e}"))?;
    *state.settings.write().map_err(|e| e.to_string())? = Settings { name: name.to_owned(), ..settings };
    restart(&app)?;
    Ok(orchestrator_settings(app))
}

/// The bearer token, for pasting into the orchestrator's MCP config. Creates it if needed.
#[tauri::command]
pub fn orchestrator_reveal_token() -> Result<String> {
    token(true).ok_or_else(|| "Could not create the orchestrator token.".into())
}

/// The user hands a task to the orchestrator (`supervisor` = its name) or takes it over (`null`).
#[tauri::command]
pub async fn set_task_supervisor(app: AppHandle, state: State<'_, AppState>, session_id: String, supervisor: Option<String>) -> Result<crate::storage::Session> {
    let name = settings(&app).name;
    if supervisor.as_deref().is_some_and(|s| s != name) {
        return Err(format!("Only {name} can supervise tasks."));
    }
    let store = state.store.clone();
    let before = store.lock().map_err(|e| e.to_string())?.session(&session_id)?;
    let session = store.lock().map_err(|e| e.to_string())?.set_supervisor(&session_id, supervisor.as_deref())?;
    let _ = app.emit("sessions-changed", ());
    match (before.supervisor.as_deref(), supervisor.as_deref()) {
        (Some(_), None) => hooks::notify_with(&app, &before, hooks::Event::TakenOver),
        (None, Some(_)) => hooks::notify(&app, &session_id, hooks::Event::HandedOver { last_reply: String::new() }),
        _ => {}
    }
    Ok(session)
}

#[cfg(test)]
mod tests {
    use super::clamp_mode;

    #[test]
    fn modes_are_capped_at_the_ceiling() {
        assert_eq!(clamp_mode(Some("full"), "ask", "auto"), ("auto".into(), true));
        assert_eq!(clamp_mode(Some("plan"), "ask", "auto"), ("plan".into(), false));
        assert_eq!(clamp_mode(None, "ask", "auto"), ("ask".into(), false));
        assert_eq!(clamp_mode(Some("bogus"), "full", "auto"), ("auto".into(), true));
        assert_eq!(clamp_mode(Some("full"), "ask", "full"), ("full".into(), false));
    }
}
