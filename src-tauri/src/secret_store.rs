//! The OS secure store (Keychain, Secret Service, Credential Manager) for app secrets.
//! Values never enter prompts, logs, the database or the webview.
//! The headless core (remote mode) usually runs where no keyring is unlocked, so it
//! keeps secrets in an owner-only file in its data folder instead (`file`).

#[cfg(feature = "headless")]
#[cfg_attr(test, allow(dead_code))] // tests use the in-memory store
pub(crate) mod file {
    use std::{collections::BTreeMap, path::PathBuf, sync::{Mutex, OnceLock}};

    static PATH: OnceLock<PathBuf> = OnceLock::new();
    static LOCK: Mutex<()> = Mutex::new(());

    /// Called once at startup with the data folder.
    pub(crate) fn use_folder(dir: &std::path::Path) {
        let _ = PATH.set(dir.join("secrets.json"));
    }
    fn read(path: &std::path::Path) -> BTreeMap<String, String> {
        std::fs::read(path).ok().and_then(|bytes| serde_json::from_slice(&bytes).ok()).unwrap_or_default()
    }
    fn update(change: impl FnOnce(&mut BTreeMap<String, String>)) -> crate::Result<()> {
        let path = PATH.get().ok_or("The secret file is not configured.")?;
        let _guard = LOCK.lock().map_err(|_| "The secret file is busy.")?;
        let mut map = read(path);
        change(&mut map);
        let temporary = path.with_extension("json.tmp");
        let bytes = serde_json::to_vec_pretty(&map).map_err(|e| e.to_string())?;
        let mut options = std::fs::OpenOptions::new();
        options.write(true).create(true).truncate(true);
        #[cfg(unix)]
        std::os::unix::fs::OpenOptionsExt::mode(&mut options, 0o600);
        std::io::Write::write_all(&mut options.open(&temporary).map_err(|e| e.to_string())?, &bytes).map_err(|e| e.to_string())?;
        std::fs::rename(&temporary, path).map_err(|_| "Could not save the secret file.".into())
    }
    pub(crate) fn get(key: &str) -> Option<String> {
        let path = PATH.get()?;
        let _guard = LOCK.lock().ok()?;
        read(path).remove(key)
    }
    pub(crate) fn set(key: &str, value: &str) -> crate::Result<()> {
        update(|map| { map.insert(key.to_owned(), value.to_owned()); })
    }
    pub(crate) fn remove(key: &str) -> crate::Result<()> {
        update(|map| { map.remove(key); })
    }
}

#[cfg(test)]
fn memory() -> &'static std::sync::Mutex<std::collections::HashMap<String, String>> {
    static STORE: std::sync::OnceLock<std::sync::Mutex<std::collections::HashMap<String, String>>> = std::sync::OnceLock::new();
    STORE.get_or_init(Default::default)
}

pub(crate) fn set(service: &str, account: &str, value: &str) -> crate::Result<()> {
    let value = value.trim();
    if value.is_empty() || value.len() > 4096 || value.chars().any(char::is_control) {
        return Err("That secret looks empty or malformed.".into());
    }
    #[cfg(test)]
    {
        memory().lock().unwrap().insert(format!("{service}/{account}"), value.to_owned());
        Ok(())
    }
    #[cfg(all(not(test), feature = "headless"))]
    {
        file::set(&format!("{service}/{account}"), value)
    }
    #[cfg(all(not(test), not(feature = "headless"), target_os = "macos"))]
    {
        security_framework::passwords::set_generic_password(service, account, value.as_bytes()).map_err(|_| "Could not save the secret in Keychain.".into())
    }
    #[cfg(all(not(test), not(feature = "headless"), any(target_os = "linux", target_os = "windows")))]
    {
        keyring::Entry::new(service, account).and_then(|entry| entry.set_password(value)).map_err(|_| "Could not save the secret in secure storage.".into())
    }
    #[cfg(all(not(test), not(feature = "headless"), not(any(target_os = "macos", target_os = "linux", target_os = "windows"))))]
    {
        let _ = (service, account, value);
        Err("Secure storage is not available on this platform.".into())
    }
}

pub(crate) fn get(service: &str, account: &str) -> Option<String> {
    #[cfg(test)]
    {
        memory().lock().unwrap().get(&format!("{service}/{account}")).cloned()
    }
    #[cfg(all(not(test), feature = "headless"))]
    {
        file::get(&format!("{service}/{account}"))
    }
    #[cfg(all(not(test), not(feature = "headless"), target_os = "macos"))]
    {
        match security_framework::passwords::get_generic_password(service, account) {
            Ok(bytes) => String::from_utf8(bytes).ok(),
            // -25300 is "not found"; anything else (a denied or dismissed access prompt) is worth logging.
            Err(error) if error.code() == -25300 => None,
            Err(error) => {
                eprintln!("Keychain read for {service} failed ({}): {error}", error.code());
                None
            }
        }
    }
    #[cfg(all(not(test), not(feature = "headless"), any(target_os = "linux", target_os = "windows")))]
    {
        keyring::Entry::new(service, account).and_then(|entry| entry.get_password()).ok()
    }
    #[cfg(all(not(test), not(feature = "headless"), not(any(target_os = "macos", target_os = "linux", target_os = "windows"))))]
    {
        let _ = (service, account);
        None
    }
}

pub(crate) fn remove(service: &str, account: &str) {
    #[cfg(test)]
    {
        memory().lock().unwrap().remove(&format!("{service}/{account}"));
    }
    #[cfg(all(not(test), feature = "headless"))]
    {
        let _ = file::remove(&format!("{service}/{account}"));
    }
    #[cfg(all(not(test), not(feature = "headless"), target_os = "macos"))]
    {
        let _ = security_framework::passwords::delete_generic_password(service, account);
    }
    #[cfg(all(not(test), not(feature = "headless"), any(target_os = "linux", target_os = "windows")))]
    {
        if let Ok(entry) = keyring::Entry::new(service, account) {
            let _ = entry.delete_credential();
        }
    }
    #[cfg(all(not(test), not(feature = "headless"), not(any(target_os = "macos", target_os = "linux", target_os = "windows"))))]
    {
        let _ = (service, account);
    }
}
