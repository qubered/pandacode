# Remote core: headless backend, remote frontend

Status: **Phases 1–4 are done** (option B), verified end to end on an Ubuntu
26.04 x86_64 server over Tailscale (see "Verification").
Remote mode is a third run mode, beside native (Tauri) and preview (browser fixtures).

## Running it

On the server X:

1. Build the binary, on any machine with Docker:
   ```bash
   docker build -f docker/pandacode-core.Dockerfile --platform linux/amd64 --target export --output dist-core .
   ```
   Use `linux/arm64` for an ARM server. This needs glibc 2.35 or newer
   (Ubuntu 22.04+, Debian 12+).
2. On X, install the runtime libraries and copy the binary over:
   ```bash
   sudo apt install libgtk-3-0 git
   ```
   You also need the coding CLIs you use (logged in as the same user).
3. Start the core on the tailnet address:
   ```bash
   ./pandacode-core --bind "$(tailscale ip -4)" --port 7878
   ```
   - Data defaults to `~/.local/share/ai.pandacode.desktop` (change it with
     `--data-dir`).
   - Secrets go in `secrets.json` there, with mode 0600.
   - Run it under systemd or tmux so it outlives your SSH session. On SIGTERM it
     stops agents cleanly.

On the laptop Y:

1. Serve the frontend: `npm run dev` (it serves `http://127.0.0.1:1420`).
   `npm run build && npm run preview` serves the built frontend instead.
2. Open `http://127.0.0.1:1420/?core=ws://<X-tailnet-ip>:7878`.

The core accepts browser pages only from loopback origins, which covers the
frontend served locally on Y. To serve the frontend from another host, start the
core with `--allow-origin http://that-host:port`. This is not a login. It stops a
website open in your browser from driving the core: browsers apply no CORS to
WebSockets, and the browser sits inside the tailnet.

For a local trial on one machine, use `npm run core -- --port 7878` and
`?core=ws://127.0.0.1:7878`.

**From the desktop app instead of a browser:**
1. Open Settings → General → Remote core.
2. Enter `ws://<X-tailnet-ip>:7878` and click **Connect**. The address is checked
   first, then remembered across launches.
3. A chip in the header shows the server and whether it's connected.

What happens in this mode:
- Tasks, projects, terminals and agents run on X.
- The window's appearance, zoom, app updates and links stay on your computer.
- Task exports are saved on your computer.
- Dropped files aren't sent to the server. Paste images, or attach files from the
  server instead.
- **Use this computer**, in Settings or on the "can't reach" panel, switches back
  to the app's own core.

The core accepts the desktop app's origins (`tauri://localhost`,
`http(s)://tauri.localhost`) without `--allow-origin`.

## Verification (2026-10-22)

**Automated tests:**
- `npm run test:rust`: 983 passed.
- `npm run test:rust:headless`: 948 passed. This includes:
  - WebSocket dispatch, typed errors, headless stubs, Origin checks and event
    filtering.
  - Terminal streaming and re-attachment after a reconnect.
  - A check that every emitted or subscribed channel is forwarded.
- `npm test`: 108 passed, including the transport's reconnect and no-replay
  tests.
- `npx tsc --noEmit` and `npm run build` are clean.
- The Playwright suites for attachments, moodboards, provider settings, settings
  and layout pass, so the preview path is intact.

**Linux core (`debian:bookworm-slim`, only `libgtk-3-0` and `git`, no display):**
- WebKit: zero packages installed, and none in the binary's `NEEDED` list.
- In the browser, against this core:
  - The remote picker browsed `/root` and opened a project.
  - A terminal ran `uname`, reporting `Linux aarch64`.
  - After a page reload, the reopened terminal adopted the same shell. It
    showed output printed while no page was attached.

**On a real server (Ubuntu 26.04 x86_64, systemd user service on the tailnet IP,
frontend on a Mac connecting over Tailscale):**
- A Claude Code task in Standard mode sent its approval request to the browser
  (`sleep 25 && date > done.txt`). It was approved there.
- The page was reloaded while the command ran. It reattached to the running
  task and the restored tool activity.
- The agent finished, `done.txt` was on the server, and its reply streamed in.

**Earlier agent check (local core on macOS):**
- **Working:** a prompt reached the Claude Code and Codex CLIs, and their
  replies streamed back as `agent-event`s, including errors and a provider
  handoff.
- **Not verified:** a completed reply and the approval round trip. This Mac's
  Claude Code isn't logged in, and its Codex CLI's models are refused for the
  account. Re-check both on X.

Goal: run PandaCode's Rust core headless on a server X (no webview, no display),
run the frontend on a laptop Y, and connect Y to X over a WebSocket on a
Tailscale network. Agent runs keep going on X when Y disconnects; reopening Y
reattaches to the live state. No auth, TLS or multi-tenancy: binding to the
tailnet interface is the security boundary, and there is a single user.

## 1. What the code looks like (verified against `30f9c82`, v0.3.16)

| Fact | Brief said | Verified |
|---|---|---|
| `#[tauri::command]` fns | ~241 | **241**, all registered in the main-window `generate_handler!` in `lib.rs::run()`. A second, 11-command handler serves the `launcher` (overlay) webview. Any other webview is rejected. |
| Emit sites | ~97 | **97** `.emit(`/`.emit_to(` calls, plus helper fns (`architect::emit`, `android_setup::emit`) that wrap them. |
| Event channels | ~16 | **~25** distinct channels: the brief's 16 plus `terminal-event`, `sessions-deleted`, `agent-ui-action` (an `emit_to("main", …)`), `agent-update-progress`, `jev-install`, `overlay-visibility`, and the browser/jev frame channels. The subscribed set is already listed in `src/bridge.ts`. |
| Frontend boundary | one file | Mostly true. `call`/`subscribe` in `bridge.ts` carry all IPC, but **7 other files import Tauri APIs directly** (see §4), and **13 files branch on `preview`**. |

### 1.1 How the commands depend on Tauri

Parameter types across the 241 registered commands:

| Tauri parameters | Commands |
|---|---|
| none (plain args only) | 37 |
| `State<AppState>` (or another managed state) only | 106 |
| `AppHandle` only | 50 |
| `AppHandle` + `State` | 35 |
| includes `WebviewWindow` | 13 |

The 13 `WebviewWindow` parameters are all the same guard,
`if window.label() != "main" { return Err(…) }`. None of them draws anything.

### 1.2 How deep the `AppHandle` coupling goes

`AppHandle` is not only used to emit events. It is the app's service locator:

- **205 references in 37 of 144 files.** The heaviest users are `lib.rs`, `jev.rs`,
  `browser.rs`, `bots*`, `architect.rs`, `overlay.rs`, `simulator/`, and `vm/`.
- **110 `app.state::<T>()` lookups in 27 files.** Managed state is `AppState`
  (store, agents, terminals, shutdown), `browser::BrowserState`,
  `simulator::Simulators`, `vm::Vms`, `file_preview::FilePreviews`,
  `agent_tools::UiRequests`, `architect::Runtime`, plus overlay and bots state.
- **The agent run path threads the handle through 7 tool hosts.**
  `launch_operation_admitted` (in `lib.rs`) passes `app.clone()` into
  `architect::host`, `agent_tools::host`/`with_jev`/`with_preview`,
  `bots::tools::with_bot`, `simulator::tools::with_simulator`, `vm::tools::with_vm`,
  and `cloud::deploy::with_deploy`. It also uses `app.path().app_cache_dir()` for
  screenshots and turn-change captures.
- **The provider runtime itself is clean.** `providers.rs` and `providers/` never
  touch Tauri: events leave through a sink closure that `lib.rs` wires to
  `app.emit("agent-event", …)`. Spawning CLIs, parsing streams, approvals and
  steering are already Tauri-free.
- `tauri::async_runtime::spawn` appears 31 times in 19 files. It is a thin
  wrapper over tokio.

## 2. The decision this phase needs: what is `AppCtx`?

The brief asks for an `AppCtx` that Tauri and headless both satisfy. Two ways to
build it differ by an order of magnitude in churn, so I need your call
before Phase 2.

### Option A: hand-rolled `AppCtx` with no Tauri in headless

Define `trait AppCtx { fn emit(…); fn state<T>(…); fn data_dir(); … }`, implement
it for `tauri::AppHandle` and for a headless struct, and rewrite every module that
takes `AppHandle`/`State` to take the context instead.

- **Result:** a headless binary with no `tauri` crate and no GTK.
- **Cost:**
  - Rewrites ~205 `AppHandle` sites and 110 `state::<T>()` lookups. It needs a
    typed state container to replace Tauri's.
  - Needs **a hand-written dispatcher for ~176 commands**: one args struct per
    command, matching Tauri's camelCase→snake_case argument renaming and its
    `State` injection. That is the same work `generate_handler!` does today, so
    it duplicates Tauri, and the two copies can drift silently.
  - Every upstream change to a command signature becomes a merge conflict in
    the fork. Upstream ships often (16 releases in 0.3.x).

### Option B (recommended): `AppCtx` = `tauri::AppHandle<Rt>`, with Tauri's mock runtime in headless

Tauri 2 can build an `App` against `tauri::test::MockRuntime`, a runtime with no
windowing and no webview. With that runtime, `generate_handler!`,
`State<T>`, `app.state()`, `app.emit()`, `listen_any` and the path resolver all
keep working.

- **Shared type:** make the runtime a crate-level alias:
  ```rust
  #[cfg(feature = "desktop")]  pub type Rt = tauri::Wry;
  #[cfg(feature = "headless")] pub type Rt = tauri::test::MockRuntime;
  pub type AppHandle = tauri::AppHandle<Rt>;   // this is the AppCtx
  ```
- **Code changes:** `tauri::AppHandle` → `crate::AppHandle` (mechanical, ~205
  sites, no logic change), plus the 6 explicit `Wry` references.
- **Dispatch:** the WS server builds a `tauri::webview::InvokeRequest`
  `{cmd, body: json args}` and hands it to `Webview::on_message` on a mock
  `"main"` webview. The **same** `generate_handler!` table runs the **same**
  handlers, with Tauri's own argument parsing and the existing `"main"` label
  guards.
- **Events:** `app.listen_any(channel, …)` on every channel forwards to all WS
  clients. A unit test greps `src/**/*.rs` for emitted channel literals and fails
  if one is missing from the forward list.
- **Costs and risks:**
  - **On Linux the `tauri` crate always links GTK 3 (`gtk`, `muda`), even with
    `wry` off.** X needs `libgtk-3-0` installed. It does **not** need
    WebKitGTK, a display server, or a window: the mock runtime never calls
    `gtk_init`. This meets "no webkitgtk, no window" literally, but not "no GUI
    libraries on the box". *I have not built this on Linux yet; a container
    spike is the first step of Phase 2.*
  - `tauri::test` is a public, documented API, but it is aimed at tests. Its
    behavior is pinned by `Cargo.lock` (tauri 2.11.5) and covered by our own
    dispatch tests.
- **Fallback:** if the spike shows a blocker, Option A is still available, and
  Option B's alias work is not wasted: A can start module by module from it.

#### Spike result (Debian bookworm, aarch64 container, Rust 1.99, tauri 2.11.5 / tauri-runtime 2.11.3)

The spike used a minimal crate with `tauri = { default-features = false, features = ["test"] }`
and versions pinned to this repo's lockfile. It built a mock app with managed
state and a `"main"` mock webview, then sent commands through `Webview::on_message`.

- **Works headless with no display** (`DISPLAY`/`WAYLAND_DISPLAY` unset):
  - `generate_handler!` dispatch, including async commands, `State<T>`,
    `AppHandle<MockRuntime>`, `WebviewWindow` label (`"main"`), errors, and
    "Command … not found".
  - camelCase → snake_case argument renaming.
  - Both `emit()` and `emit_to("main", …)` reach `listen_any`.
  - 8 concurrent calls from separate threads.
- **Build time:** `tauri-runtime` depends on `webkit2gtk` on Linux
  **unconditionally** (its public types mention `webkit2gtk::WebView`). Building
  therefore needs `libgtk-3-dev` **and `libwebkit2gtk-4.1-dev`** headers, even
  with `wry` off.
- **Run time:** Rust links with `--as-needed`, so the binary's `NEEDED` list is
  only GLib/GObject/GIO, GTK 3/GDK, Pango, Cairo, gdk-pixbuf and libc. **No
  WebKit at run time.** On `debian:bookworm-slim`, `libgtk-3-0` adds ~180 MB.
- **The mock context has no bundle identifier,** so `app_data_dir()` resolves
  to `~/.local/share/`. Headless must set the identifier to
  `ai.pandacode.desktop` (or pass `--data-dir`).
- **Version skew breaks the build:** a fresh resolve picks `tauri-runtime`
  2.12, which does not compile against `tauri` 2.11.5. The repo's `Cargo.lock`
  already pins compatible versions; keep it.

Practical consequence: build `pandacode-core` in a Docker builder stage (or CI)
that has the dev headers, and ship a runtime image or host with only
`libgtk-3-0`. X itself never needs WebKitGTK.

### Cargo layout (both options)

- `default = ["desktop"]`.
- `desktop` enables `tauri/wry` and the default Tauri features, plus the dialog,
  global-shortcut, updater and process plugins and the direct Linux deps
  `webkit2gtk`, `cairo-rs` and `gio`. These become optional.
- `headless = ["tauri/test"]`.
- New bin `src/bin/pandacode-core.rs` with `required-features = ["headless"]`.
  It takes flags `--bind 127.0.0.1` (set it to the tailnet IP), `--port 7878`, and
  `--data-dir` (default: the desktop app's own data dir,
  `~/.local/share/ai.pandacode.desktop`, so `kingcode.sqlite3` is shared).
- `browser`, `overlay`, `appearance`, `simulator` and `vm` compile only under
  `desktop`. Their tool hosts in `launch_operation_admitted` and the setup calls
  in `run()` get `#[cfg(feature = "desktop")]`.

## 3. Command categories

The counts below are the category column of the appendix:

| Category | Count | Headless treatment |
|---|---|---|
| **(a) core/portable**: tasks, chat, queue, approvals, turn changes, providers, git/worktrees, GitHub, files, terminals, architect, bots, moodboards, usage, imports | **176** | Dispatched unchanged |
| **(b) webview-bound**: `browser_*` (7), `overlay_*` (11), `configure_window_appearance` | **19** | Stub with `"<cmd> is unavailable in headless mode"` (appearance returns `Ok` so the UI stays quiet) |
| **(c) host/OS**: simulators (10), VMs (8), cloud (9), Jev (13+4), file preview, external URLs, path-taking imports | **46** | See below |

Decisions in (c) that I propose (yours to override):

- **Stub:** `simulator_*`, `android_setup` and `vm_*`. They need macOS or Apple
  silicon and the host's display.
- **Stub for now, port later:** `cloud_*` and `jev*`. They are portable in
  principle (REST, CLIs, a browser runtime), but each needs secrets or a
  frame/preview pipe to Y.
- **Stub for now:** `prepare_file_preview`. It serves HTML from a `127.0.0.1`
  server on X that Y cannot reach. A later fix is to proxy it over the WS.
- **Handled on Y:** `open_external_url` and `open_support` never reach X. Remote
  mode calls `window.open` on Y instead.
- **Port, with paths picked on X through the Phase 3 picker:** `theme_import`,
  `import_moodboard_images`, `read_document`, `describe_folder`,
  `provider_set_executable`, and `token_saver_install` (which installs `rtk` on
  X, the machine that runs agents).

## 4. Frontend plan (Phase 1), including what the brief didn't list

- **`src/remote-transport.ts`**, as specified:
  - Frames: request `{id, command, args}`, response `{id, ok, result|error}`,
    event `{event, payload}`.
  - Reconnects with backoff. Subscriptions are kept client-side and
    re-registered after a reconnect.
  - In-flight calls reject with a clear error when the socket drops; they are
    not silently replayed.
- **`bridge.ts`:**
  - Add `remote` and a lazy transport, and route `call`/`subscribe` to the
    transport when `remote` is set.
  - **`preview` must become `!remote && !TAURI`.** Today it is just
    `!TAURI`, so in a plain browser it would turn on fixture behavior in 13
    files.
  - Add `native = !remote && !preview` for the native-only paths.
- **Direct Tauri imports outside `bridge.ts` that remote mode must route around:**

  | File | Native API | Remote behavior |
  |---|---|---|
  | `main.ts` (export task) | `plugin-dialog.save` | Browser download of the Markdown via a Blob |
  | `provider-settings.ts` | `plugin-dialog.open` (agent executable) | Remote picker on X |
  | `moodboards.ts` | `plugin-dialog.open` (images) | `<input type=file>` on Y, sent through the existing `add_moodboard_image` `data_url` path |
  | `attachments.ts` | webview drag-drop | HTML5 drop: images become `data_url` (`import_prompt_image` already accepts it); other files show a "pick on server" hint |
  | `ui-size.ts` | webview zoom | CSS zoom fallback |
  | `about-settings.ts` | `app.getVersion` | Version from a `core_info` command |
  | `app-updates.ts` | updater/process | Disabled in remote mode (X updates by redeploying the binary) |

- **`chooseFolder`, `chooseAttachments`, `chooseVsix`, `chooseAttachmentFolder`:**
  in remote mode they open an in-app file browser dialog backed by the Phase 3
  commands.

## 5. Phase 3 commands (remote filesystem)

- `fs_home() -> String`
- `fs_list(path, show_hidden?) -> { path, parent, entries: [{ name, kind, size }] }`

The browsing, filtering, multi-select and folder-vs-file logic live in the
frontend dialog. Both commands are registered in desktop builds too, so preview
mocks and tests can cover them.

## 6. Reconnect and "keeps running on X"

- **Runs:** agent runs are tokio tasks owned by the core process, and their
  state is in SQLite. A frontend disconnect does not touch them. On reconnect the
  app's normal boot (`list_sessions`, `get_messages`, `queue_list`, …) rebuilds
  the UI, and live `agent-event`s resume.
- **Approvals:** these are persisted (`save_approval`), and every connected
  client sees them. The first `respond_approval` wins.
- **UI actions:** `agent-ui-action` requests from agents (`emit_to("main")`)
  time out after 12 s when no client is connected. That is existing behavior.
- **Gap, terminals:** there is **no terminal scrollback buffer**. Output exists
  only as `terminal-event`s, so after a reconnect the shell is still alive but
  its earlier output is gone. Proposal: a per-terminal ring buffer (~256 KB) and
  a `terminal_snapshot(id)` command the terminal view calls on attach.
- **Gap, streaming text:** to verify in Phase 4 how much of an in-progress
  assistant message `get_messages` returns mid-turn (see `flush_assistant` in
  `lib.rs`).

## 7. Other headless blockers found

- **Secrets:** on Linux, `secret_store.rs` uses `keyring` with the D-Bus Secret
  Service. A headless server usually has no unlocked keyring. Bots channels and
  cloud keys use this store. Proposal: under `headless`, use a `0600` JSON file
  in the data dir.
- **Claude credentials:** `usage_keychain.rs` is macOS-only. On Linux, Claude
  Code keeps its login in `~/.claude/.credentials.json` and the usage panel
  degrades gracefully. The CLI itself works.
- **Shutdown:** `run()`'s `ExitRequested` handler (stop agents, wait up to 12 s,
  `providers::shutdown_all()`) moves into a shared function. Headless calls it
  on SIGINT/SIGTERM.
- **Toolchain (this Mac):** the default `stable` here is Rust 1.80.1, which cannot
  build the current lockfile (crates need edition 2024). Use `cargo +1.98.1` or
  `rustup update stable`.

## 8. Open questions

1. Option **B** (mock runtime, needs GTK 3 libs on X) or Option **A** (hand-rolled
   `AppCtx`, no Tauri in headless, much larger diff)?
2. Add the terminal scrollback buffer and `terminal_snapshot` (small change)?
3. File-backed secret store under `headless`?
4. Accept the (c) dispositions in §3: cloud and Jev stubbed for now, bots ported?
5. Phase 4 needs a Linux host. Do you want me to verify against X directly (give
   me its tailnet name/SSH), or in a local Linux container first?

## Appendix: every registered command

Cat.: **a** core/portable · **b** webview-bound · **c** host/OS. "Tauri params"
are the injected parameters; **Webview** is always the `"main"` label guard.

| Command | Module | Tauri params | Cat. | Headless plan |
|---|---|---|---|---|
| `open_support` | `support.rs:8` | — | c | Frontend: open on Y with `window.open`; never reaches X |
| `support_details` | `support.rs:21` | — | a | Port |
| `check_agent_updates` | `agent_updates.rs:539` | Webview | a | Port |
| `update_agent` | `agent_updates.rs:561` | App, Webview | a | Port |
| `list_moodboards` | `moodboards.rs:328` | State | a | Port |
| `create_moodboard` | `moodboards.rs:336` | State | a | Port |
| `read_moodboard` | `moodboards.rs:352` | State | a | Port |
| `rename_moodboard` | `moodboards.rs:368` | State | a | Port |
| `delete_moodboard` | `moodboards.rs:391` | State | a | Port |
| `add_moodboard_image` | `moodboards.rs:409` | State | a | Port |
| `import_moodboard_images` | `moodboards.rs:430` | State | c | Port: paths come from `fs_pick` on X |
| `update_moodboard_image` | `moodboards.rs:455` | State | a | Port |
| `remove_moodboard_image` | `moodboards.rs:492` | State | a | Port |
| `moodboard_image` | `moodboards.rs:521` | State | a | Port |
| `overlay_preferences` | `overlay.rs:294` | State | b | Stub: global-shortcut launcher window |
| `overlay_save_preferences` | `overlay.rs:303` | App, State | b | Stub: global-shortcut launcher window |
| `overlay_recording` | `overlay.rs:347` | App, State | b | Stub: global-shortcut launcher window |
| `overlay_sync_context` | `overlay.rs:400` | State | b | Stub: global-shortcut launcher window |
| `overlay_toggle` | `overlay.rs:539` | App | b | Stub: global-shortcut launcher window |
| `overlay_hide` | `overlay.rs:466` | App | b | Stub: global-shortcut launcher window |
| `overlay_snapshot` | `overlay.rs:710` | State | b | Stub: global-shortcut launcher window |
| `overlay_thread` | `overlay.rs:896` | State | b | Stub: global-shortcut launcher window |
| `overlay_launch` | `overlay.rs:913` | App, State | b | Stub: global-shortcut launcher window |
| `overlay_select_model` | `overlay.rs:420` | State | b | Stub: global-shortcut launcher window |
| `overlay_open_task` | `overlay.rs:1078` | App, State | b | Stub: global-shortcut launcher window |
| `provider_configuration` | `provider_settings.rs:328` | — | a | Port |
| `agent_usage` | `agent_usage.rs:518` | Webview | a | Port |
| `usage_ledger` | `usage_ledger.rs:332` | App, Webview | a | Port |
| `prompt_activity` | `lib.rs:519` | State | a | Port |
| `set_resume_at_reset` | `lib.rs:511` | State | a | Port |
| `clear_usage_limit` | `lib.rs:515` | State | a | Port |
| `discard_draft` | `lib.rs:588` | App, Webview | a | Port |
| `queue_list` | `message_queue.rs:150` | State | a | Port |
| `queue_add` | `message_queue.rs:156` | State | a | Port |
| `queue_update` | `message_queue.rs:162` | State | a | Port |
| `queue_remove` | `message_queue.rs:168` | State | a | Port |
| `queue_reorder` | `message_queue.rs:174` | State | a | Port |
| `queue_take` | `message_queue.rs:180` | State | a | Port |
| `queue_restore` | `message_queue.rs:186` | State | a | Port |
| `steer_task` | `message_queue.rs:194` | State | a | Port |
| `bots_list` | `bots.rs:554` | State | a | Port; channel secrets need the headless secret store |
| `bot_desktop_thread` | `bots.rs:560` | App, State | a | Port; channel secrets need the headless secret store |
| `bot_detail` | `bots.rs:574` | State | a | Port; channel secrets need the headless secret store |
| `bot_create` | `bots.rs:589` | App, State, Webview | a | Port; channel secrets need the headless secret store |
| `bot_update` | `bots.rs:597` | App, State, Webview | a | Port; channel secrets need the headless secret store |
| `bot_set_paused` | `bots.rs:605` | App, State, Webview | a | Port; channel secrets need the headless secret store |
| `bot_reset` | `bots.rs:616` | App, State, Webview | a | Port; channel secrets need the headless secret store |
| `bot_delete` | `bots.rs:626` | App, State, Webview | a | Port; channel secrets need the headless secret store |
| `bot_remember` | `bots.rs:637` | App, State | a | Port; channel secrets need the headless secret store |
| `bot_forget` | `bots.rs:644` | App, State | a | Port; channel secrets need the headless secret store |
| `bot_activity` | `bots.rs:651` | State | a | Port; channel secrets need the headless secret store |
| `bot_project_ids` | `bots.rs:656` | State | a | Port; channel secrets need the headless secret store |
| `bot_message` | `bots.rs:662` | App | a | Port; channel secrets need the headless secret store |
| `bot_schedule_create` | `bots/schedule.rs:227` | App, State | a | Port; channel secrets need the headless secret store |
| `bot_schedule_set_enabled` | `bots/schedule.rs:234` | App, State | a | Port; channel secrets need the headless secret store |
| `bot_schedule_delete` | `bots/schedule.rs:241` | App, State | a | Port; channel secrets need the headless secret store |
| `bot_run_schedule_now` | `bots/schedule.rs:248` | App, State | a | Port; channel secrets need the headless secret store |
| `bot_channel_add` | `bots/channels/mod.rs:451` | App, State | a | Port; channel secrets need the headless secret store |
| `bot_channel_test` | `bots/channels/mod.rs:499` | — | a | Port; channel secrets need the headless secret store |
| `bot_channel_configure` | `bots/channels/mod.rs:491` | App, State | a | Port; channel secrets need the headless secret store |
| `bot_channel_set_enabled` | `bots/channels/mod.rs:504` | App, State | a | Port; channel secrets need the headless secret store |
| `bot_channel_remove` | `bots/channels/mod.rs:516` | App, State | a | Port; channel secrets need the headless secret store |
| `bot_pairing_decide` | `bots/channels/mod.rs:528` | App, State | a | Port; channel secrets need the headless secret store |
| `bot_web_link` | `bots/channels/mod.rs:545` | App, State | a | Port; channel secrets need the headless secret store |
| `bot_web_new_link` | `bots/channels/mod.rs:557` | App, State | a | Port; channel secrets need the headless secret store |
| `bot_rule_add` | `bots/guard.rs:122` | App, State | a | Port; channel secrets need the headless secret store |
| `bot_rule_delete` | `bots/guard.rs:129` | App, State | a | Port; channel secrets need the headless secret store |
| `list_importable_conversations` | `conversation_import.rs:157` | App, Webview | a | Port |
| `import_conversations` | `conversation_import.rs:236` | App, Webview | a | Port |
| `provider_set_claude_connection_mode` | `provider_settings.rs:563` | App | a | Port |
| `provider_claude_status` | `providers/claude_auth.rs:116` | — | a | Port |
| `list_opencode_models` | `provider_settings.rs:453` | — | a | Port |
| `save_opencode_models` | `provider_settings.rs:467` | App | a | Port |
| `provider_set_executable` | `provider_settings.rs:388` | App | a | Port: path comes from `fs_pick` on X |
| `provider_save_glm_key` | `provider_settings.rs:500` | — | a | Port |
| `provider_remove_glm_key` | `provider_settings.rs:509` | — | a | Port |
| `provider_save_mimo_key` | `provider_settings.rs:518` | — | a | Port |
| `provider_remove_mimo_key` | `provider_settings.rs:527` | — | a | Port |
| `provider_save_deepseek_key` | `provider_settings.rs:536` | — | a | Port |
| `provider_remove_deepseek_key` | `provider_settings.rs:545` | — | a | Port |
| `provider_set_mimo_endpoint` | `provider_settings.rs:588` | App | a | Port |
| `architect_profiles` | `architect/profiles.rs:302` | State | a | Port |
| `architect_save_profile` | `architect/profiles.rs:307` | State | a | Port |
| `architect_delete_profile` | `architect/profiles.rs:317` | State | a | Port |
| `architect_get` | `architect.rs:445` | State | a | Port |
| `architect_configure` | `architect.rs:455` | App, State | a | Port |
| `architect_start` | `architect.rs:644` | App, State | a | Port |
| `architect_control` | `architect.rs:747` | App, State | a | Port |
| `architect_worker_messages` | `architect.rs:851` | State | a | Port |
| `architect_team_messages` | `architect/coordination.rs:137` | State | a | Port |
| `task_goal` | `task_controls.rs:61` | App, State | a | Port |
| `codex_command` | `task_controls.rs:160` | State | a | Port |
| `codex_jobs` | `task_controls.rs:175` | State | a | Port |
| `set_task_preference` | `task_controls.rs:233` | State | a | Port |
| `fork_codex_task` | `task_controls.rs:411` | State | a | Port |
| `fork_session_at_message` | `task_controls.rs:380` | App, State | a | Port |
| `get_message_content` | `task_controls.rs:397` | State | a | Port |
| `archive_codex_task` | `task_controls.rs:512` | State | a | Port |
| `export_task` | `task_controls.rs:534` | State | a | Port |
| `save_task_export` | `task_controls.rs:569` | State | a | Port |
| `start_harness_terminal` | `task_controls.rs:612` | App, State | a | Port |
| `list_projects` | `lib.rs:474` | State | a | Port |
| `add_project` | `lib.rs:478` | State | a | Port |
| `remove_project` | `lib.rs:486` | State | a | Port |
| `list_sessions` | `lib.rs:501` | State | a | Port |
| `pin_session` | `lib.rs:524` | App, State | a | Port |
| `reorder_sessions` | `lib.rs:530` | App, State | a | Port |
| `get_task` | `lib.rs:537` | State | a | Port |
| `configure_window_appearance` | `lib.rs:547` | — | b | Stub (no-op Ok): native window chrome |
| `create_session` | `lib.rs:562` | State | a | Port |
| `create_board_task` | `lib.rs:603` | State | a | Port |
| `update_task_details` | `lib.rs:623` | State | a | Port |
| `set_task_stage` | `lib.rs:640` | State | a | Port |
| `start_task` | `lib.rs:1103` | App, State | a | Port |
| `configure_session` | `lib.rs:656` | State | a | Port |
| `set_reasoning_effort` | `lib.rs:675` | State | a | Port |
| `update_session` | `lib.rs:693` | State | a | Port |
| `delete_session` | `task_deletion.rs:93` | App, Webview | a | Port |
| `get_messages` | `lib.rs:725` | State | a | Port |
| `turn_change_diff` | `turn_changes.rs:1623` | State | a | Port |
| `turn_change_summary` | `turn_changes.rs:1614` | State | a | Port |
| `list_subagents` | `lib.rs:738` | State | a | Port |
| `send_message` | `lib.rs:1061` | App, State | a | Port |
| `list_slash_commands` | `lib.rs:968` | State | a | Port |
| `provider_check_connection` | `providers/acp.rs:827` | — | a | Port |
| `resolve_slash_command` | `lib.rs:989` | State | a | Port |
| `stop_session` | `lib.rs:1928` | App, State | a | Port |
| `respond_approval` | `lib.rs:1953` | State | a | Port |
| `respond_user_input` | `lib.rs:1982` | State | a | Port |
| `list_agent_integrations` | `lib.rs:2023` | State | a | Port |
| `list_app_tools` | `lib.rs:2032` | — | a | Port |
| `open_external_url` | `lib.rs:2041` | — | c | Frontend: open on Y with `window.open`; never reaches X |
| `agent_ui_request_active` | `agent_tools.rs:848` | State | a | Port |
| `resolve_agent_ui_action` | `agent_tools.rs:861` | State | a | Port |
| `browser_inspect` | `browser.rs:426` | App | b | Stub: needs an embedded child webview |
| `list_providers` | `lib.rs:1966` | — | a | Port |
| `list_provider_models` | `lib.rs:1976` | — | a | Port |
| `git_status` | `lib.rs:2079` | State | a | Port |
| `git_diff` | `lib.rs:2088` | State | a | Port |
| `git_stage` | `lib.rs:2099` | State | a | Port |
| `git_commit` | `lib.rs:2110` | State | a | Port |
| `git_branches` | `lib.rs:2275` | State | a | Port |
| `git_switch_branch` | `lib.rs:2286` | State | a | Port |
| `git_push` | `lib.rs:2303` | State | a | Port |
| `create_worktree` | `lib.rs:2168` | State | a | Port |
| `attach_worktree` | `lib.rs:2184` | State | a | Port |
| `worktree_status` | `lib.rs:2224` | State | a | Port |
| `remove_worktree` | `lib.rs:2247` | State | a | Port |
| `merge_worktree` | `lib.rs:2234` | State | a | Port |
| `recreate_worktree` | `lib.rs:2264` | State | a | Port |
| `github_status` | `lib.rs:2383` | State | a | Port |
| `github_repos` | `lib.rs:2354` | — | a | Port |
| `project_icon` | `lib.rs:2349` | State | a | Port |
| `github_repo_pulls` | `lib.rs:2358` | — | a | Port |
| `github_repo_issues` | `lib.rs:2362` | — | a | Port |
| `github_repo_runs` | `lib.rs:2366` | — | a | Port |
| `github_project_repos` | `lib.rs:2371` | State | a | Port |
| `github_clone_repo` | `lib.rs:2377` | State | a | Port |
| `github_search` | `lib.rs:2393` | State | a | Port |
| `github_issues` | `lib.rs:2404` | State | a | Port |
| `github_resolve` | `lib.rs:2414` | State | a | Port |
| `github_refresh_links` | `lib.rs:2433` | State | a | Port |
| `github_create_pull_request` | `lib.rs:2474` | State | a | Port |
| `github_pull_request` | `lib.rs:2514` | State | a | Port |
| `github_pull_request_diff` | `lib.rs:2520` | State | a | Port |
| `github_merge_pull_request` | `lib.rs:2527` | State | a | Port |
| `github_detect_pull_request` | `lib.rs:2540` | State | a | Port |
| `github_link_pull_request` | `lib.rs:2582` | State | a | Port |
| `simulator_status` | `simulator/commands.rs:12` | App | c | Stub: iOS Simulator / Android emulator on the host Mac |
| `simulator_select` | `simulator/commands.rs:17` | App | c | Stub: iOS Simulator / Android emulator on the host Mac |
| `simulator_create_duo` | `simulator/commands.rs:28` | App | c | Stub: iOS Simulator / Android emulator on the host Mac |
| `simulator_pose` | `simulator/commands.rs:40` | App | c | Stub: iOS Simulator / Android emulator on the host Mac |
| `simulator_power` | `simulator/commands.rs:55` | App | c | Stub: iOS Simulator / Android emulator on the host Mac |
| `simulator_frame` | `simulator/commands.rs:74` | App | c | Stub: iOS Simulator / Android emulator on the host Mac |
| `simulator_input` | `simulator/commands.rs:90` | App | c | Stub: iOS Simulator / Android emulator on the host Mac |
| `simulator_appearance` | `simulator/commands.rs:101` | — | c | Stub: iOS Simulator / Android emulator on the host Mac |
| `simulator_capture` | `simulator/commands.rs:108` | App | c | Stub: iOS Simulator / Android emulator on the host Mac |
| `android_setup` | `simulator/android_setup.rs:104` | App | c | Stub: iOS Simulator / Android emulator on the host Mac |
| `vm_status` | `vm/commands.rs:34` | App | c | Stub: Tart VMs need Apple silicon |
| `vm_show` | `vm/commands.rs:39` | App | c | Stub: Tart VMs need Apple silicon |
| `vm_create` | `vm/commands.rs:50` | App | c | Stub: Tart VMs need Apple silicon |
| `vm_power` | `vm/commands.rs:66` | App | c | Stub: Tart VMs need Apple silicon |
| `vm_frame` | `vm/commands.rs:89` | App | c | Stub: Tart VMs need Apple silicon |
| `vm_input` | `vm/commands.rs:99` | App | c | Stub: Tart VMs need Apple silicon |
| `vm_desktop` | `vm/commands.rs:107` | App | c | Stub: Tart VMs need Apple silicon |
| `vm_setup` | `vm/setup.rs:70` | — | c | Stub: Tart VMs need Apple silicon |
| `cloud_connections` | `cloud/commands.rs:34` | — | c | Defer: portable REST/CLI, but keys need the headless secret store; stub first |
| `cloud_connect` | `cloud/commands.rs:40` | App | c | Defer: portable REST/CLI, but keys need the headless secret store; stub first |
| `cloud_disconnect` | `cloud/commands.rs:180` | App | c | Defer: portable REST/CLI, but keys need the headless secret store; stub first |
| `cloud_setup_sandboxes` | `cloud/commands.rs:153` | App | c | Defer: portable REST/CLI, but keys need the headless secret store; stub first |
| `cloud_machines` | `cloud/commands.rs:270` | — | c | Defer: portable REST/CLI, but keys need the headless secret store; stub first |
| `cloud_create` | `cloud/commands.rs:351` | App | c | Defer: portable REST/CLI, but keys need the headless secret store; stub first |
| `cloud_power` | `cloud/commands.rs:360` | App | c | Defer: portable REST/CLI, but keys need the headless secret store; stub first |
| `cloud_frame` | `cloud/commands.rs:369` | — | c | Defer: portable REST/CLI, but keys need the headless secret store; stub first |
| `cloud_input` | `cloud/commands.rs:373` | — | c | Defer: portable REST/CLI, but keys need the headless secret store; stub first |
| `themes_list` | `vsix_themes.rs:238` | App | a | Port |
| `theme_import` | `vsix_themes.rs:253` | App | c | Port: path is on X; route the picker through `fs_pick` |
| `theme_install` | `vsix_themes.rs:313` | App | a | Port |
| `theme_remove` | `vsix_themes.rs:325` | App | a | Port |
| `token_saver_tools` | `token_saver.rs:86` | — | a | Port |
| `token_saver_install` | `token_saver.rs:52` | — | c | Port: installs `rtk` on X (the host that runs agents) |
| `set_task_link` | `lib.rs:2459` | State | a | Port |
| `import_prompt_image` | `lib.rs:2315` | App | a | Port |
| `read_document` | `lib.rs:2338` | — | a | Port: paths come from `fs_pick` on X |
| `describe_folder` | `lib.rs:2343` | — | a | Port: paths come from `fs_pick` on X |
| `list_files` | `lib.rs:2600` | State | a | Port |
| `search_files` | `lib.rs:2610` | State | a | Port |
| `read_file` | `lib.rs:2621` | State | a | Port |
| `workspace_image` | `lib.rs:2631` | State | a | Port |
| `resolve_workspace_files` | `lib.rs:2638` | State | a | Port |
| `prepare_file_preview` | `lib.rs:2648` | App, State | c | Defer: serves a preview from a 127.0.0.1 server on X that Y cannot reach |
| `get_run_script` | `lib.rs:2662` | State | a | Port |
| `get_run_script_preview` | `lib.rs:2671` | App, State | a | Port |
| `start_run_script_terminal` | `lib.rs:2696` | App, State | a | Port |
| `start_terminal` | `lib.rs:2719` | App, State | a | Port |
| `write_terminal` | `lib.rs:2744` | State | a | Port |
| `resize_terminal` | `lib.rs:2753` | State | a | Port |
| `close_terminal` | `lib.rs:2774` | State | a | Port |
| `get_terminal_activity` | `lib.rs:2766` | State | a | Port |
| `get_terminal_preferences` | `lib.rs:2762` | — | a | Port |
| `get_metrics` | `lib.rs:2787` | State | a | Port |
| `list_local_servers` | `lib.rs:2828` | — | a | Port |
| `jev_configuration` | `jev.rs:471` | — | c | Defer: browser-testing runtime on the host; stub first |
| `jev_save_configuration` | `jev.rs:475` | — | c | Defer: browser-testing runtime on the host; stub first |
| `jev_doctor` | `jev.rs:715` | — | c | Defer: browser-testing runtime on the host; stub first |
| `jev_install_runtime` | `jev.rs:727` | App | c | Defer: browser-testing runtime on the host; stub first |
| `jev_start` | `jev.rs:1196` | App | c | Defer: browser-testing runtime on the host; stub first |
| `jev_watch_run` | `jev.rs:1137` | App | c | Defer: browser-testing runtime on the host; stub first |
| `jev_cancel` | `jev.rs:1133` | App | c | Defer: browser-testing runtime on the host; stub first |
| `jev_list_runs` | `jev.rs:1121` | App | c | Defer: browser-testing runtime on the host; stub first |
| `jev_read_run` | `jev.rs:1125` | App | c | Defer: browser-testing runtime on the host; stub first |
| `jev_assist_prepare` | `jev_decisions.rs:972` | App | c | Defer: browser-testing runtime on the host; stub first |
| `jev_assist_run` | `jev_decisions.rs:980` | App | c | Defer: browser-testing runtime on the host; stub first |
| `jev_assist_list` | `jev_decisions.rs:988` | App | c | Defer: browser-testing runtime on the host; stub first |
| `jev_assist_save_rules` | `jev_decisions.rs:992` | App | c | Defer: browser-testing runtime on the host; stub first |
| `browser_open` | `browser.rs:232` | App | b | Stub: needs an embedded child webview |
| `browser_bounds` | `browser.rs:344` | App | b | Stub: needs an embedded child webview |
| `browser_action` | `browser.rs:369` | App | b | Stub: needs an embedded child webview |
| `browser_pick` | `browser.rs:480` | App | b | Stub: needs an embedded child webview |
| `browser_capture` | `browser.rs:584` | App | b | Stub: needs an embedded child webview |
| `browser_close` | `browser.rs:755` | App | b | Stub: needs an embedded child webview |
