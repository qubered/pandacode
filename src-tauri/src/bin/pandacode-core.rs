//! `pandacode-core`: PandaCode's core without a window, for remote mode
//! (docs/remote-core.md). Open the frontend with `?core=ws://<address>:<port>`.

const USAGE: &str = "Usage: pandacode-core [--bind ADDRESS] [--port PORT] [--data-dir DIR]

  --bind ADDRESS   Interface to listen on (default 127.0.0.1). Use your tailnet IP to
                   reach it from another machine; there is no authentication or TLS.
  --port PORT      WebSocket port (default 7878).
  --data-dir DIR   Task database and settings (default: the desktop app's data folder).
  --allow-origin ORIGIN
                   Also accept browser pages from ORIGIN (repeatable), such as
                   http://laptop:1420. Pages on localhost are always accepted; other
                   pages are refused so a website you visit cannot drive the core.";

fn main() {
    pandacode_lib::prepare_environment();
    let mut bind = "127.0.0.1".to_string();
    let mut port = 7878u16;
    let mut data_dir = None;
    let mut allowed_origins = Vec::new();
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        let mut value = |name: &str| args.next().unwrap_or_else(|| fail(&format!("{name} needs a value.")));
        match arg.as_str() {
            "--bind" => bind = value("--bind"),
            "--port" => port = value("--port").parse().unwrap_or_else(|_| fail("--port must be a number from 1 to 65535.")),
            "--data-dir" => data_dir = Some(std::path::PathBuf::from(value("--data-dir"))),
            "--allow-origin" => allowed_origins.push(value("--allow-origin")),
            "-h" | "--help" => {
                println!("{USAGE}");
                return;
            }
            other => fail(&format!("Unknown option {other}.")),
        }
    }
    let ip: std::net::IpAddr = bind.parse().unwrap_or_else(|_| fail("--bind must be an IP address, such as your tailnet IP."));
    if let Err(error) = pandacode_lib::remote_core::serve(pandacode_lib::remote_core::Options {
        address: (ip, port).into(),
        data_dir,
        allowed_origins,
    }) {
        fail(&error);
    }
}

fn fail(message: &str) -> ! {
    eprintln!("{message}\n\n{USAGE}");
    std::process::exit(2)
}
