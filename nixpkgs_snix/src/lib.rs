use std::collections::HashMap;
use std::rc::Rc;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};

use bstr::ByteSlice;
use clap::Parser;
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use snix_build::buildservice::DummyBuildService;
use snix_eval::builtins::impure_builtins;
use snix_eval::{EvalIO, Evaluation, Value};
use snix_glue::builtins::{add_derivation_builtins, add_fetcher_builtins, add_import_builtins};
use snix_glue::configure_nix_path;
use snix_glue::snix_io::SnixIO;
use snix_glue::snix_store_io::SnixStoreIO;
use snix_store::utils::{ServiceUrlsMemory, construct_services};

type SnixErr = Box<dyn std::error::Error + Send + Sync>;

fn py_err(e: impl std::fmt::Display) -> PyErr {
    PyRuntimeError::new_err(e.to_string())
}

fn make_snix_io() -> Result<(Rc<SnixStoreIO>, tokio::runtime::Runtime), SnixErr> {
    let runtime = tokio::runtime::Runtime::new()?;
    let (blob, dir, path_info, nar) = runtime.block_on(construct_services(
        ServiceUrlsMemory::parse_from(std::iter::empty::<&str>()),
    ))?;
    let io = Rc::new(SnixStoreIO::new(
        blob,
        dir,
        path_info,
        nar.into(),
        Arc::<DummyBuildService>::default(),
        runtime.handle().clone(),
        Vec::new(),
    ));
    Ok((io, runtime))
}

fn snix_eval_expr(nixpkgs_path: &str, expr: &str) -> Result<Value, SnixErr> {
    let (store_io, _rt) = make_snix_io()?;
    let nix_path = format!("nixpkgs={nixpkgs_path}");

    let mut builder = Evaluation::builder(
        Box::new(SnixIO::new(store_io.clone() as Rc<dyn EvalIO>)) as Box<dyn EvalIO>,
    )
    .add_builtins(impure_builtins())
    .enable_import();

    builder = add_derivation_builtins(builder, Rc::clone(&store_io));
    builder = add_fetcher_builtins(builder, Rc::clone(&store_io));
    builder = add_import_builtins(builder, Rc::clone(&store_io));
    builder = configure_nix_path(builder, &Some(nix_path));

    let result = builder.build().evaluate(expr, None);

    if !result.errors.is_empty() {
        return Err(result
            .errors
            .iter()
            .map(|e| e.to_string())
            .collect::<Vec<_>>()
            .join("; ")
            .into());
    }

    result
        .value
        .ok_or_else(|| "evaluation produced no value".into())
}

static TEMP_DIR_COUNTER: AtomicU64 = AtomicU64::new(0);

/// Evaluate each `.nix` file in `files` as a Nix attrset and return the
/// first `version` string found, or None.  Handles packages that store
/// their version in a companion file rather than inline, without
/// hard-coding the companion filename.
#[pyfunction]
fn find_version_in_files(
    py: Python<'_>,
    files: HashMap<String, String>,
) -> PyResult<Option<String>> {
    py.allow_threads(move || {
        let id = TEMP_DIR_COUNTER.fetch_add(1, Ordering::Relaxed);
        let temp_dir = std::env::temp_dir().join(format!("nixpkgs-snix-{id}"));
        std::fs::create_dir_all(&temp_dir).map_err(py_err)?;

        for (name, content) in &files {
            std::fs::write(temp_dir.join(name), content).map_err(py_err)?;
        }

        let dir_str = temp_dir
            .to_str()
            .ok_or_else(|| py_err("temp dir path is not UTF-8"))?
            .to_owned();

        let mut result = None;
        for name in files.keys() {
            if !name.ends_with(".nix") {
                continue;
            }
            let file_path = temp_dir.join(name);
            let Some(file_str) = file_path.to_str() else {
                continue;
            };
            let expr = format!(
                "let x = import {file_str}; \
                 in if builtins.isAttrs x && x ? version then x.version else null"
            );
            if let Ok(Value::String(s)) = snix_eval_expr(&dir_str, &expr)
                && let Ok(ver) = s.as_bstr().to_str()
                && !ver.is_empty()
            {
                result = Some(ver.to_owned());
                break;
            }
        }

        let _ = std::fs::remove_dir_all(&temp_dir);
        Ok(result)
    })
}

#[pymodule]
fn nixpkgs_snix(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(find_version_in_files, m)?)?;
    Ok(())
}
