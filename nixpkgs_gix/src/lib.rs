use std::sync::Once;

use bstr::ByteSlice;
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;

fn py_err(e: impl std::fmt::Display) -> PyErr {
    PyRuntimeError::new_err(e.to_string())
}

static INTERRUPT_INIT: Once = Once::new();

fn ensure_interrupt_handler() {
    INTERRUPT_INIT.call_once(|| {
        unsafe { gix::interrupt::init_handler(1, || {}).ok() };
    });
}

/// Parse a full ref name and, if it looks like a NixOS release branch,
/// return `((year, month), "nixos-YY.MM")`.
fn parse_release_ref(full_ref: &str) -> Option<((u32, u32), String)> {
    let suffix = full_ref
        .strip_prefix("refs/remotes/origin/nixos-")
        .or_else(|| full_ref.strip_prefix("refs/heads/nixos-"))?
        .to_owned();
    let (year_str, month_str) = suffix.split_once('.')?;
    let year: u32 = year_str.parse().ok()?;
    let month: u32 = month_str.parse().ok()?;
    Some(((year, month), format!("nixos-{suffix}")))
}

/// Fetch only the refs needed by nixpkgs-radar, using targeted refspecs rather than
/// the default "+refs/heads/*:refs/remotes/origin/*". The wildcard refspec causes gix
/// to update thousands of remote-tracking refs in one transaction, which fails on large
/// repos like nixpkgs. Targeted refspecs keep the ref-update set small and conflict-free.
fn try_fetch(path: &str) -> Result<(), String> {
    let repo = gix::open(path).map_err(|e| e.to_string())?;
    let remote = repo
        .find_default_remote(gix::remote::Direction::Fetch)
        .ok_or_else(|| "repo has no default remote".to_string())?
        .map_err(|e| e.to_string())?
        .with_refspecs(
            [
                "+refs/heads/master:refs/remotes/origin/master",
                "+refs/heads/nixpkgs-unstable:refs/remotes/origin/nixpkgs-unstable",
                "+refs/heads/nixos-*:refs/remotes/origin/nixos-*",
            ],
            gix::remote::Direction::Fetch,
        )
        .map_err(|e| e.to_string())?;
    remote
        .connect(gix::remote::Direction::Fetch)
        .map_err(|e| e.to_string())?
        .prepare_fetch(gix::progress::Discard, Default::default())
        .map_err(|e| e.to_string())?
        .receive(gix::progress::Discard, &gix::interrupt::IS_INTERRUPTED)
        .map_err(|e| e.to_string())?;
    Ok(())
}

fn do_clone(url: String, path: &str, url_display: &str) -> PyResult<()> {
    std::fs::create_dir_all(path).map_err(py_err)?;
    let (mut prepare_checkout, _) = gix::prepare_clone(url, path)
        .map_err(|e| PyRuntimeError::new_err(format!("prepare clone ({url_display}): {e:#}")))?
        .fetch_then_checkout(gix::progress::Discard, &gix::interrupt::IS_INTERRUPTED)
        .map_err(|e| {
            PyRuntimeError::new_err(format!("fetch then checkout ({url_display}): {e:#}"))
        })?;
    prepare_checkout
        .main_worktree(gix::progress::Discard, &gix::interrupt::IS_INTERRUPTED)
        .map_err(|e| PyRuntimeError::new_err(format!("main worktree ({url_display}): {e:#}")))?;
    Ok(())
}

/// Clone a repo if the path does not yet exist, or fetch targeted refs if it does.
/// Never removes an existing checkout - on fetch failure the error is propagated to
/// the caller, which decides whether to retry or raise a permanent ApplicationFailure.
#[pyfunction]
fn clone_or_fetch_repo(py: Python<'_>, url: String, path: String) -> PyResult<()> {
    ensure_interrupt_handler();

    let url_display = url.clone();
    let is_existing = {
        let p = std::path::Path::new(&path);
        p.join(".git").exists() || p.join("HEAD").exists()
    };

    py.allow_threads(move || {
        if is_existing {
            try_fetch(&path)
                .map_err(|e| PyRuntimeError::new_err(format!("fetch ({url_display}): {e}")))?;
        } else {
            do_clone(url, &path, &url_display)?;
        }
        Ok(())
    })
}

/// Inspect the remote-tracking refs of a cloned nixpkgs repo and return the
/// name of the latest stable release branch (e.g. "release-26.05").
#[pyfunction]
fn find_latest_nixos_release(repo_path: String) -> PyResult<String> {
    let repo = gix::open(&repo_path).map_err(py_err)?;
    let refs = repo.references().map_err(py_err)?;

    refs.all()
        .map_err(py_err)?
        .filter_map(|r| r.ok())
        .filter_map(|r| {
            let full = r.name().as_bstr().to_str().ok()?.to_owned();
            parse_release_ref(&full)
        })
        .max_by_key(|(ver, _)| *ver)
        .map(|(_, name)| name)
        .ok_or_else(|| PyRuntimeError::new_err("no release-XX.YY branches found in repo"))
}

/// Read all `.nix` blobs in `dir_path` (relative to the repo root) at
/// `ref_name` directly from the git object database.
///
/// Returns a map of filename → content. Returns an empty map if the ref or
/// directory does not exist.
#[pyfunction]
fn read_dir_at_ref(
    repo_path: String,
    dir_path: String,
    ref_name: String,
) -> PyResult<std::collections::HashMap<String, String>> {
    let repo = gix::open(&repo_path).map_err(py_err)?;

    let spec = format!("{ref_name}:{dir_path}");
    let tree_obj = match repo.rev_parse_single(spec.as_str()) {
        Ok(obj) => obj
            .object()
            .map_err(py_err)?
            .try_into_tree()
            .map_err(|_| PyRuntimeError::new_err(format!("{spec} is not a tree")))?,
        Err(_) => return Ok(std::collections::HashMap::new()),
    };

    let names: Vec<String> = {
        let decoded = tree_obj.decode().map_err(py_err)?;
        decoded
            .entries
            .iter()
            .filter(|e| e.mode.is_blob() && e.filename.to_str_lossy().ends_with(".nix"))
            .map(|e| e.filename.to_str_lossy().into_owned())
            .collect()
    };
    drop(tree_obj);

    let mut files = std::collections::HashMap::new();
    for name in names {
        let file_spec = format!("{ref_name}:{dir_path}/{name}");
        if let Ok(obj) = repo.rev_parse_single(file_spec.as_str()) {
            if let Ok(blob) = obj.object().map_err(py_err)?.try_into_blob() {
                files.insert(name, String::from_utf8_lossy(&blob.data).into_owned());
            }
        }
    }
    Ok(files)
}

/// Walk `pkgs/` at HEAD and return every `.nix` blob whose content contains
/// `github_handle` as a literal substring.
///
/// Returns a map of relative path → file content, read entirely from the git
/// object DB.
#[pyfunction]
fn find_files_by_handle(
    py: Python<'_>,
    repo_path: String,
    github_handle: String,
) -> PyResult<std::collections::HashMap<String, String>> {
    ensure_interrupt_handler();

    py.allow_threads(|| {
        let repo = gix::open(&repo_path).map_err(py_err)?;

        let pkgs_oid = match repo.rev_parse_single("HEAD:pkgs") {
            Ok(id) => id.detach(),
            Err(_) => return Ok(std::collections::HashMap::new()),
        };

        struct Entry {
            oid: gix::ObjectId,
            is_blob: bool,
            path: String,
        }

        let mut stack: Vec<(gix::ObjectId, String)> = vec![(pkgs_oid, "pkgs".to_owned())];
        let mut result = std::collections::HashMap::new();

        while let Some((tree_oid, prefix)) = stack.pop() {
            let obj = match repo.find_object(tree_oid) {
                Ok(o) => o,
                Err(_) => continue,
            };
            let tree = match obj.try_into_tree() {
                Ok(t) => t,
                Err(_) => continue,
            };

            let entries: Vec<Entry> = {
                let Ok(decoded) = tree.decode() else { continue };
                decoded
                    .entries
                    .iter()
                    .map(|e| Entry {
                        oid: e.oid.to_owned(),
                        is_blob: e.mode.is_blob(),
                        path: format!("{}/{}", prefix, e.filename.to_str_lossy()),
                    })
                    .collect()
            };
            drop(tree); // release ODB buffer borrow before further find_object calls

            for e in entries {
                if e.is_blob {
                    if !e.path.ends_with(".nix") {
                        continue;
                    }
                    let Ok(blob_obj) = repo.find_object(e.oid) else {
                        continue;
                    };
                    let Ok(blob) = blob_obj.try_into_blob() else {
                        continue;
                    };
                    let content = String::from_utf8_lossy(&blob.data);
                    if content.contains(github_handle.as_str()) {
                        result.insert(e.path, content.into_owned());
                    }
                } else {
                    stack.push((e.oid, e.path));
                }
            }
        }

        Ok(result)
    })
}

/// Read the raw content of `file_path` (relative to the repo root) at
/// `ref_name` (e.g. "refs/remotes/origin/nixpkgs-unstable") directly from
/// the git object database without requiring a working-tree checkout.
///
/// Returns None if the ref or the path does not exist at that ref.
#[pyfunction]
fn get_file_at_ref(
    repo_path: String,
    file_path: String,
    ref_name: String,
) -> PyResult<Option<String>> {
    let repo = gix::open(&repo_path).map_err(py_err)?;

    let spec = format!("{ref_name}:{file_path}");
    let object = match repo.rev_parse_single(spec.as_str()) {
        Ok(obj) => obj,
        Err(_) => return Ok(None),
    };

    let blob = object
        .object()
        .map_err(py_err)?
        .try_into_blob()
        .map_err(|_| PyRuntimeError::new_err(format!("{spec} is not a blob")))?;

    Ok(Some(String::from_utf8_lossy(&blob.data).into_owned()))
}

#[pymodule]
fn nixpkgs_gix(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(clone_or_fetch_repo, m)?)?;
    m.add_function(wrap_pyfunction!(find_latest_nixos_release, m)?)?;
    m.add_function(wrap_pyfunction!(read_dir_at_ref, m)?)?;
    m.add_function(wrap_pyfunction!(get_file_at_ref, m)?)?;
    m.add_function(wrap_pyfunction!(find_files_by_handle, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_remote_tracking_ref() {
        let r = parse_release_ref("refs/remotes/origin/nixos-26.05");
        assert_eq!(r, Some(((26, 5), "nixos-26.05".into())));
    }

    #[test]
    fn parse_local_branch_ref() {
        let r = parse_release_ref("refs/heads/nixos-25.11");
        assert_eq!(r, Some(((25, 11), "nixos-25.11".into())));
    }

    #[test]
    fn non_release_refs_ignored() {
        assert!(parse_release_ref("refs/remotes/origin/master").is_none());
        assert!(parse_release_ref("refs/remotes/origin/nixpkgs-unstable").is_none());
        assert!(parse_release_ref("refs/heads/main").is_none());
    }

    #[test]
    fn malformed_release_ref_ignored() {
        assert!(parse_release_ref("refs/remotes/origin/nixos-notanumber.05").is_none());
        assert!(parse_release_ref("refs/remotes/origin/nixos-26").is_none());
    }

    #[test]
    fn picks_latest_from_multiple() {
        let refs = [
            "refs/remotes/origin/nixos-24.11",
            "refs/remotes/origin/nixos-25.05",
            "refs/remotes/origin/nixos-26.05",
            "refs/remotes/origin/nixos-25.11",
        ];
        let latest = refs
            .iter()
            .filter_map(|r| parse_release_ref(r))
            .max_by_key(|(ver, _)| *ver)
            .map(|(_, name)| name);
        assert_eq!(latest.as_deref(), Some("nixos-26.05"));
    }
}
