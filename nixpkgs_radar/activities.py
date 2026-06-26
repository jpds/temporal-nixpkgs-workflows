from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request

from temporalio import activity

LOG = logging.getLogger(__name__)

_NIXPKGS_URL_DEFAULT = "https://github.com/NixOS/nixpkgs"
_VERSION_RE = re.compile(r'\bversion\s*=\s*"([^"]+)"')
_GITHUB_RE = re.compile(r"https?://(?:www\.)?github\.com/([^/\s]+)/([^/\s?#]+)")
_GITLAB_COM_RE = re.compile(r"https?://gitlab\.com/([^/\s]+)/([^/\s?#]+)")
_GITEA_HOMEPAGE_RE = re.compile(r"https?://([^/\s]+)/([^/\s?#]+)/([^/\s?#]+)")
_GITEA_DOMAINS = frozenset({"gitea.com", "codeberg.org", "notabug.org"})
_PRERELEASE_RE = re.compile(
    r"[-._]?(alpha|beta|rc|pre|preview|dev)\d*(?:$|[-._])", re.IGNORECASE
)


def _nixpkgs_path() -> str:
    return os.environ.get(
        "NIXPKGS_CACHE_PATH",
        os.path.expanduser("~/.cache/temporal-nixpkgs-radar/nixpkgs"),
    )


def _nixpkgs_url() -> str:
    return os.environ.get("NIXPKGS_URL", _NIXPKGS_URL_DEFAULT)


@contextlib.contextmanager
def _heartbeat_every(seconds: float = 10.0):
    ctx = contextvars.copy_context()
    stop = threading.Event()

    def _loop():
        while not stop.wait(seconds):
            try:
                ctx.run(activity.heartbeat)
            except Exception:
                pass

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()
        t.join(timeout=seconds + 1)




def _parse_fetch_attrs(block: str) -> dict[str, str]:
    return dict(re.findall(r'(\w+)\s*=\s*"([^"]*)"', block))


_FETCH_BLOCK = r"((?:[^{}]|\$\{[^{}]*\})*)"


def _extract_forge_info_from_nix(content: str) -> tuple | None:
    m = re.search(rf"fetchFromGitHub\s*\{{{_FETCH_BLOCK}\}}", content, re.DOTALL)
    if m:
        attrs = _parse_fetch_attrs(m.group(1))
        owner, repo = attrs.get("owner", ""), attrs.get("repo", "")
        if owner and repo:
            return ("github", "github.com", owner, repo)

    m = re.search(rf"fetchFromGitLab\s*\{{{_FETCH_BLOCK}\}}", content, re.DOTALL)
    if m:
        attrs = _parse_fetch_attrs(m.group(1))
        owner = attrs.get("owner") or attrs.get("group", "")
        repo = attrs.get("repo", "")
        host = attrs.get("domain", "gitlab.com")
        if owner and repo:
            return ("gitlab", host, owner, repo)

    m = re.search(rf"fetchFromGitea\s*\{{{_FETCH_BLOCK}\}}", content, re.DOTALL)
    if m:
        attrs = _parse_fetch_attrs(m.group(1))
        host = attrs.get("domain", "gitea.com")
        owner, repo = attrs.get("owner", ""), attrs.get("repo", "")
        if owner and repo:
            return ("gitea", host, owner, repo)

    return None


def _parse_homepage_forge(homepage: str) -> tuple | None:
    m = _GITHUB_RE.match(homepage)
    if m:
        return ("github", "github.com", m.group(1), m.group(2).rstrip("/"))

    m = _GITLAB_COM_RE.match(homepage)
    if m:
        return ("gitlab", "gitlab.com", m.group(1), m.group(2).rstrip("/"))

    m = _GITEA_HOMEPAGE_RE.match(homepage)
    if m:
        domain, owner, repo = m.group(1), m.group(2), m.group(3).rstrip("/")
        if domain in _GITEA_DOMAINS:
            return ("gitea", domain, owner, repo)

    return None


def _is_prerelease_version(version: str) -> bool:
    return bool(_PRERELEASE_RE.search(version))


def _fetch_github_version(repo: str, headers: dict, api_base: str) -> dict | None:
    def _get(url: str):
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())

    stable_version: str | None = None
    releases_exist = True

    try:
        stable_data = _get(f"{api_base}/repos/{repo}/releases/latest")
        stable_tag = stable_data.get("tag_name", "")
        stable_version = stable_tag.removeprefix("v") or None
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            releases_exist = False
            LOG.debug("no GitHub releases for %s, trying tags", repo)
        elif exc.code in (403, 429):
            LOG.warning("GitHub rate-limited (%d) for %s", exc.code, repo)
            raise
        else:
            LOG.warning("GitHub API error %d for %s", exc.code, repo)
            return None
    except Exception as exc:
        LOG.warning("GitHub upstream check failed for %s: %s", repo, exc)
        return None

    if not releases_exist or not stable_version:
        try:
            tags = _get(f"{api_base}/repos/{repo}/tags?per_page=1")
            if tags:
                stable_version = tags[0].get("name", "").removeprefix("v") or None
        except Exception as exc:
            LOG.debug("GitHub tags fallback failed for %s: %s", repo, exc)
        if not stable_version:
            return None
        return {
            "version": stable_version,
            "is_prerelease": False,
            "stable_version": None,
        }

    try:
        latest_list = _get(f"{api_base}/repos/{repo}/releases?per_page=1")
        if latest_list and latest_list[0].get("prerelease"):
            pre_tag = latest_list[0].get("tag_name", "")
            pre_version = pre_tag.removeprefix("v") or None
            if pre_version and pre_version != stable_version:
                return {
                    "version": pre_version,
                    "is_prerelease": True,
                    "stable_version": stable_version,
                }
    except Exception:
        pass

    return {"version": stable_version, "is_prerelease": False, "stable_version": None}


def _fetch_gitlab_version(host: str, owner: str, repo: str) -> dict | None:
    encoded = urllib.parse.quote(f"{owner}/{repo}", safe="")
    url = f"https://{host}/api/v4/projects/{encoded}/releases?per_page=10"
    headers: dict[str, str] = {"User-Agent": "temporal-nixpkgs-radar/1.0"}
    token = os.environ.get("GITLAB_TOKEN")
    if token:
        headers["PRIVATE-TOKEN"] = token

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            releases = json.loads(r.read())
    except Exception as exc:
        LOG.warning("GitLab API error for %s/%s/%s: %s", host, owner, repo, exc)
        return None

    if not releases:
        return None

    latest_version: str | None = None
    latest_is_pre = False
    stable_version: str | None = None

    for release in releases:
        tag = release.get("tag_name", "")
        version = tag.removeprefix("v")
        if not version:
            continue
        is_pre = _is_prerelease_version(version)
        if latest_version is None:
            latest_version = version
            latest_is_pre = is_pre
        if not is_pre and stable_version is None:
            stable_version = version
        if latest_version and stable_version:
            break

    if latest_version is None:
        return None

    if latest_is_pre:
        return {
            "version": latest_version,
            "is_prerelease": True,
            "stable_version": stable_version,
        }
    return {"version": latest_version, "is_prerelease": False, "stable_version": None}


def _fetch_gitea_version(host: str, owner: str, repo: str) -> dict | None:
    headers = {"User-Agent": "temporal-nixpkgs-radar/1.0"}

    def _get(url: str):
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())

    try:
        stable_releases = _get(
            f"https://{host}/api/v1/repos/{owner}/{repo}/releases?limit=1&pre-release=false"
        )
        stable_version = None
        if stable_releases:
            tag = stable_releases[0].get("tag_name", "")
            stable_version = tag.removeprefix("v") or None
    except Exception as exc:
        LOG.warning("Gitea API error for %s/%s/%s: %s", host, owner, repo, exc)
        return None

    if not stable_version:
        return None

    try:
        latest_releases = _get(
            f"https://{host}/api/v1/repos/{owner}/{repo}/releases?limit=1"
        )
        if latest_releases and latest_releases[0].get("prerelease"):
            pre_tag = latest_releases[0].get("tag_name", "")
            pre_version = pre_tag.removeprefix("v") or None
            if pre_version and pre_version != stable_version:
                return {
                    "version": pre_version,
                    "is_prerelease": True,
                    "stable_version": stable_version,
                }
    except Exception:
        pass

    return {"version": stable_version, "is_prerelease": False, "stable_version": None}


@activity.defn
def clone_or_fetch_nixpkgs() -> None:
    from temporalio.exceptions import ApplicationError

    import nixpkgs_gix

    url = _nixpkgs_url()
    path = _nixpkgs_path()
    has_checkout = os.path.exists(os.path.join(path, "HEAD")) or os.path.exists(
        os.path.join(path, ".git")
    )

    with _heartbeat_every(10.0):
        try:
            nixpkgs_gix.clone_or_fetch_repo(url, path)
        except RuntimeError as exc:
            if has_checkout:
                raise ApplicationError(
                    f"fetch failed for existing nixpkgs checkout at {path!r}; "
                    f"delete it manually to allow a fresh clone: {exc}",
                    non_retryable=True,
                ) from exc
            raise

    LOG.info("nixpkgs clone/fetch complete at %s", path)


@activity.defn
def find_latest_nixos_release() -> str:
    import nixpkgs_gix

    branch = nixpkgs_gix.find_latest_nixos_release(_nixpkgs_path())
    LOG.info("Latest NixOS release branch: %s", branch)
    return branch


def _parse_package_from_nix(
    content: str, file_path: str, github_handle: str
) -> dict | None:
    if "maintainers" not in content:
        return None
    if not re.search(r'\b' + re.escape(github_handle) + r'\b', content, re.IGNORECASE):
        return None

    m = re.search(r'\bpname\s*=\s*"([^"]+)"', content)
    if m:
        pname = m.group(1)
    else:
        parts = file_path.split("/")
        filename = parts[-1]
        if filename in ("package.nix", "default.nix") and len(parts) >= 2:
            pname = parts[-2]
        else:
            pname = (
                filename.removesuffix(".nix") if filename.endswith(".nix") else filename
            )
    if not pname:
        return None

    version_m = re.search(r'\bversion\s*=\s*"([^"]+)"', content)
    version = version_m.group(1) if version_m else ""

    homepage_m = re.search(r'\bhomepage\s*=\s*"([^"]+)"', content)
    homepage = homepage_m.group(1) if homepage_m else ""

    return {
        "attr": pname,
        "pname": pname,
        "version": version,
        "homepage": homepage,
        "file": file_path,
    }


@activity.defn
def find_maintained_packages(github_handle: str) -> list[dict]:
    import nixpkgs_gix

    with _heartbeat_every(10.0):
        files = nixpkgs_gix.find_files_by_handle(_nixpkgs_path(), github_handle)
    packages, seen = [], set()
    for file_path, content in files.items():
        pkg = _parse_package_from_nix(content, file_path, github_handle)
        if pkg and pkg["file"] not in seen:
            seen.add(pkg["file"])
            packages.append(pkg)
    LOG.info("Found %d packages for %s", len(packages), github_handle)
    return packages


@activity.defn
def get_version_at_ref(
    attr: str, ref_name: str, file_path: str | None = None
) -> str | None:
    if not file_path:
        return None

    import nixpkgs_gix
    import nixpkgs_snix

    path = _nixpkgs_path()
    content = nixpkgs_gix.get_file_at_ref(path, file_path, ref_name)
    if content is None:
        return None
    m = _VERSION_RE.search(content)
    if m:
        return m.group(1)
    # Version not inline; evaluate sibling .nix files with snix.
    # Handles packages that store version in a companion file without
    # hard-coding any filename.
    parent = file_path.rsplit("/", 1)[0]
    sibling_files = nixpkgs_gix.read_dir_at_ref(path, parent, ref_name)
    if sibling_files:
        return nixpkgs_snix.find_version_in_files(sibling_files)
    return None


@activity.defn
def fetch_upstream_version(
    attr: str, homepage: str, file_path: str | None = None
) -> dict | None:
    forge_info = _parse_homepage_forge(homepage)

    if forge_info is None and file_path:
        import nixpkgs_gix

        content = nixpkgs_gix.get_file_at_ref(
            _nixpkgs_path(), file_path, "refs/remotes/origin/master"
        )
        if content is not None:
            forge_info = _extract_forge_info_from_nix(content)

    if forge_info is None:
        LOG.debug(
            "%s: cannot determine upstream forge from homepage %r", attr, homepage
        )
        return None

    forge_type, host, owner, repo = forge_info

    if forge_type == "github":
        base = os.environ.get("GITHUB_API_BASE_URL", "https://api.github.com")
        token = os.environ.get("GITHUB_TOKEN")
        headers: dict[str, str] = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "temporal-nixpkgs-radar/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        result = _fetch_github_version(f"{owner}/{repo}", headers, base)
    elif forge_type == "gitlab":
        result = _fetch_gitlab_version(host, owner, repo)
    elif forge_type == "gitea":
        result = _fetch_gitea_version(host, owner, repo)
    else:
        return None

    if result:
        LOG.info(
            "%s: upstream version %s (prerelease=%s)",
            attr,
            result["version"],
            result["is_prerelease"],
        )
    return result
