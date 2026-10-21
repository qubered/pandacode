//! Folder listings for the remote file picker (src/remote-picker.ts). In remote mode
//! projects and attachments live on the machine running the core, so the frontend
//! browses that machine's files through these commands instead of a native dialog.

use serde::Serialize;
use std::path::{Path, PathBuf};

const MAX_ENTRIES: usize = 5000;

#[derive(Serialize, Debug, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct Listing {
    path: String,
    parent: Option<String>,
    entries: Vec<Entry>,
}
#[derive(Serialize, Debug, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct Entry {
    name: String,
    path: String,
    kind: &'static str,
    size: u64,
}

fn home() -> Option<PathBuf> {
    std::env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" }).map(PathBuf::from)
}

#[tauri::command]
pub fn fs_home() -> crate::Result<String> {
    home().map(|path| path.display().to_string()).ok_or_else(|| "This machine has no home folder.".into())
}

#[tauri::command]
pub async fn fs_list(path: Option<String>, show_hidden: Option<bool>) -> crate::Result<Listing> {
    let path = match path.filter(|p| !p.trim().is_empty()) {
        Some(path) => PathBuf::from(expand_home(path.trim())),
        None => home().ok_or("This machine has no home folder.")?,
    };
    tauri::async_runtime::spawn_blocking(move || list(&path, show_hidden.unwrap_or(false)))
        .await
        .map_err(|e| e.to_string())?
}

fn expand_home(path: &str) -> PathBuf {
    match (path.strip_prefix("~"), home()) {
        (Some(rest), Some(home)) if rest.is_empty() || rest.starts_with('/') => home.join(rest.trim_start_matches('/')),
        _ => PathBuf::from(path),
    }
}

fn list(path: &Path, show_hidden: bool) -> crate::Result<Listing> {
    if !path.is_absolute() {
        return Err("Enter a full path, such as /home/you/projects.".into());
    }
    let path = path.canonicalize().map_err(|_| format!("{} does not exist.", path.display()))?;
    let read = std::fs::read_dir(&path).map_err(|error| match error.kind() {
        std::io::ErrorKind::PermissionDenied => format!("PandaCode cannot read {}.", path.display()),
        _ => format!("{} is not a folder.", path.display()),
    })?;
    let mut entries: Vec<Entry> = read
        .filter_map(|entry| entry.ok())
        .filter_map(|entry| {
            let name = entry.file_name().to_string_lossy().into_owned();
            if !show_hidden && name.starts_with('.') {
                return None;
            }
            // Follow links, so a linked folder opens like a folder.
            let metadata = std::fs::metadata(entry.path()).ok()?;
            Some(Entry {
                path: entry.path().display().to_string(),
                kind: if metadata.is_dir() { "dir" } else { "file" },
                size: if metadata.is_dir() { 0 } else { metadata.len() },
                name,
            })
        })
        .collect();
    entries.sort_by(|a, b| (a.kind != "dir").cmp(&(b.kind != "dir")).then_with(|| a.name.to_lowercase().cmp(&b.name.to_lowercase())));
    entries.truncate(MAX_ENTRIES);
    Ok(Listing {
        parent: path.parent().map(|parent| parent.display().to_string()),
        path: path.display().to_string(),
        entries,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lists_folders_first_hides_dotfiles_and_reports_the_parent() {
        let root = std::env::temp_dir().join(format!("pandacode-fs-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(root.join("b-folder")).unwrap();
        std::fs::create_dir_all(root.join(".git")).unwrap();
        std::fs::write(root.join("A.txt"), "hello").unwrap();
        let listing = list(&root, false).unwrap();
        let root = root.canonicalize().unwrap();
        assert_eq!(listing.path, root.display().to_string());
        assert_eq!(listing.parent, root.parent().map(|p| p.display().to_string()));
        let names: Vec<_> = listing.entries.iter().map(|e| (e.name.as_str(), e.kind, e.size)).collect();
        assert_eq!(names, [("b-folder", "dir", 0), ("A.txt", "file", 5)]);
        assert_eq!(list(&root, true).unwrap().entries.len(), 3);
        assert!(list(&root.join("A.txt"), false).unwrap_err().contains("not a folder"));
        assert!(list(Path::new("relative"), false).unwrap_err().contains("full path"));
        std::fs::remove_dir_all(root).unwrap();
    }
}
