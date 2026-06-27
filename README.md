# Temporal Nixpkgs Workflows

This repository contains a [Temporal](https://temporal.io) workflow that monitors
[nixpkgs](https://github.com/NixOS/nixpkgs) packages for a GitHub maintainer and reports on their
version status using a local LLM.

## How it works

### NixpkgsRadarWorkflow

Takes a GitHub handle as input and:

1. Clones or fetches the nixpkgs git repository (cached locally)
2. In parallel, determines the latest stable NixOS release branch and finds all packages
   maintained by the given GitHub handle - the latter uses `nixpkgs_gix` to search `.nix`
   files in the git object database for the maintainer handle
3. For each maintained package, fetches the version from three branches in parallel:
   - `master`
   - `nixpkgs-unstable`
   - The latest stable release (e.g. `nixos-26.05`)

   And concurrently queries the upstream forge API for the latest version - GitHub, GitLab, and
   Gitea-compatible forges are all supported.
4. Assembles a structured summary and sends it to an LLM via the OpenAI Agents SDK, which
   produces a concise, actionable report highlighting outdated packages, branch inconsistencies,
   and anything needing attention

### Native components

Two Rust extension modules (built with [maturin](https://github.com/PyO3/maturin) and
[PyO3](https://pyo3.rs)) are used for operations:

- **`nixpkgs_gix`** - git operations (clone, fetch, read file at ref, find latest release branch,
  search `.nix` files by maintainer handle) backed by [gitoxide](https://github.com/GitoxideLabs/gitoxide)
- **`nixpkgs_snix`** - Nix expression evaluation to extract version strings from companion files
  (e.g. `source.nix`) when a version is not declared inline, backed by
  [snix](https://git.snix.dev/snix/snix)

## Configuration

| Variable | Description |
|---|---|
| `OPENAI_BASE_URL` | OpenAI-compatible API base URL (e.g. `http://localhost:8080/v1` for `llama.cpp`) |
| `OPENAI_API_KEY` | API key (not needed for local models) |
| `MODEL` | Model name passed to the agent (default: `gpt-4o`) |
| `TEMPORAL_ADDRESS` | Temporal frontend address (default: `localhost:7233`) |
| `TEMPORAL_NAMESPACE` | Temporal namespace (default: `default`) |
| `TEMPORAL_TASK_QUEUE` | Task queue name (default: `nixpkgs-radar-queue`) |
| `NIXPKGS_CACHE_PATH` | Where to cache the nixpkgs clone (default: `~/.cache/temporal-nixpkgs-radar/nixpkgs`) |
| `NIXPKGS_URL` | nixpkgs remote URL (default: `https://github.com/NixOS/nixpkgs`) |
| `GITHUB_TOKEN` | GitHub personal access token for upstream version lookups (optional but avoids rate limits) |
| `GITHUB_API_BASE_URL` | GitHub API base URL (default: `https://api.github.com`) |
| `GITLAB_TOKEN` | GitLab personal access token for upstream version lookups on GitLab instances (optional) |

## Running

### Temporal server

```bash
nix-shell -p temporal-cli --run "temporal server start-dev"
```

### With Nix

```bash
nix develop

OPENAI_BASE_URL=http://localhost:8080/v1 \
OPENAI_API_KEY=not-needed \
MODEL=gemma4:e2b \
python nixpkgs-radar-worker.py
```

### Triggering the workflow

```bash
temporal workflow start \
  --type NixpkgsRadarWorkflow \
  --task-queue nixpkgs-radar-queue \
  --workflow-id nixpkgs-radar \
  --input '"your-github-handle"'

temporal workflow result --workflow-id nixpkgs-radar
```

## Testing

The NixOS VM test spins up the following in a set of VMs:

* `llama.cpp` server (using `Gemma 4 E2B QAT`)
* Temporal
* The nixpkgs-radar worker

The workflow is triggered with a test GitHub handle and the test asserts it completes with a
non-empty result.

```bash
nix flake check -L
```

To test against a local GGUF model instead of downloading one:

```nix
# In your local flake override:
model = /path/to/your/model.gguf;
```

## License

MIT
