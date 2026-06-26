from __future__ import annotations

import asyncio
import os
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    import annotated_types  # noqa: F401
    import pydantic_core  # noqa: F401
    from agents import Agent, Runner

    from nixpkgs_radar.activities import (
        clone_or_fetch_nixpkgs,
        fetch_upstream_version,
        find_latest_nixos_release,
        find_maintained_packages,
        get_version_at_ref,
    )

_MODEL = os.environ.get("MODEL", "gpt-4o")

_CLONE_TIMEOUT = timedelta(hours=1)
_EVAL_TIMEOUT = timedelta(minutes=30)
_FAST_TIMEOUT = timedelta(seconds=30)
_HEARTBEAT_TIMEOUT = timedelta(seconds=30)

_CLONE_RETRY = RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=10))
_EVAL_RETRY = RetryPolicy(maximum_attempts=2, initial_interval=timedelta(seconds=5))
_FAST_RETRY = RetryPolicy(maximum_attempts=5, initial_interval=timedelta(seconds=2))
_UPSTREAM_TIMEOUT = timedelta(minutes=10)
_UPSTREAM_RETRY = RetryPolicy(
    maximum_attempts=8,
    initial_interval=timedelta(seconds=15),
    maximum_interval=timedelta(minutes=2),
    backoff_coefficient=2.0,
)
_PACKAGE_CONCURRENCY = 3

_BRANCHES = (
    ("master", "refs/remotes/origin/master"),
    ("unstable", "refs/remotes/origin/nixpkgs-unstable"),
)

_SYSTEM_PROMPT = """
You are a NixOS package maintenance assistant helping a nixpkgs maintainer
review the packages they maintain.

For each package you are given the version found in four places:
  - nixpkgs master   : the development branch
  - nixpkgs unstable : the rolling release
  - release-YY.MM    : the current stable release branch
  - upstream latest  : the newest release published by the project
"unknown" means the data was unavailable: never treat it as out of date,
and never invent a version. Compare only the numbers you are given.

How nixpkgs updates flow: a new version lands in master/unstable first,
then qualifying changes are cherry-picked ("backported") to the stable
release branch. A backport can therefore only move a version that is
ALREADY in master/unstable down to stable - you cannot backport a version
nixpkgs does not yet have.

A package needs attention if EITHER the highest nixpkgs version trails
upstream latest, OR the stable release branch trails master/unstable. A
package is only "up to date" when its nixpkgs versions match upstream
latest; branches agreeing with each other while all trailing upstream is
NOT up to date.

Both conditions can be true for the same package - report each that
applies. Reason about each package in this order:
1. If the highest nixpkgs version trails upstream latest, recommend
   updating master to the upstream version (name it explicitly). Upstream
   is not in nixpkgs yet, so do not call this a backport and do not
   recommend backporting the upstream version directly.
2. If master/unstable already carries a version newer than the stable
   release branch, that delta IS a backport candidate. Backport the single
   highest version already present in nixpkgs (master or unstable), not
   every intermediate one. Classify the gap yourself from the numbers and
   commit to a verdict - do not ask the maintainer to confirm the
   classification:
     - Patch or minor bump with no breaking changes, or any security fix:
       recommend backporting.
     - Major bump with breaking changes: keep it in master only, UNLESS
       the package breaks when outdated (clients tied to a server-side
       protocol version, or security-critical apps such as browsers), in
       which case recommend it.
   When you recommend a backport, note that only the oldest supported
   release onward is eligible and that the maintainer should still confirm
   no breaking API changes or heavy new dependencies slipped in.
3. If upstream latest is a pre-release (RC, alpha, beta), flag it as
   something to start tracking, not to package yet.
4. Note packages present in master but missing from the stable release, or
   vice versa.

Write a concise report: one or two lines per package that needs attention,
naming the specific versions and the action you advise. Skip packages that
are fully up to date. Finish with a single-line overall summary. Do not
list healthy packages and do not pad the report.
"""


def _format_upstream(upstream_info: dict | None) -> str:
    if not upstream_info:
        return "unknown"
    version = upstream_info["version"]
    if upstream_info.get("is_prerelease"):
        stable = upstream_info.get("stable_version")
        annotation = (
            f" (pre-release; latest stable: {stable})" if stable else " (pre-release)"
        )
        return version + annotation
    return version


def _propagate_versions(
    packages: list[dict], versions: list[tuple]
) -> list[tuple]:
    best: dict[str, list] = {}
    for pkg, vers in zip(packages, versions):
        attr = pkg["attr"]
        if attr not in best:
            best[attr] = list(vers)
        else:
            for j, v in enumerate(vers):
                if v is not None and best[attr][j] is None:
                    best[attr][j] = v
    return [
        tuple(best[pkg["attr"]][j] if v is None else v for j, v in enumerate(vers))
        for pkg, vers in zip(packages, versions)
    ]


def _format_packages(
    packages: list[dict],
    versions: list[tuple],
    latest_release: str,
) -> str:
    lines: list[str] = []
    for pkg, (master_v, unstable_v, stable_v, upstream_info) in zip(
        packages, versions, strict=True
    ):
        file_path = pkg.get("file", "")
        pkg_dir = file_path.rsplit("/", 1)[0] if "/" in file_path else file_path
        lines.append(
            f"- {pkg['attr']} [{pkg_dir}] (homepage: {pkg['homepage']})\n"
            f"    nixpkgs master    : {master_v or 'unknown'}\n"
            f"    nixpkgs unstable  : {unstable_v or 'unknown'}\n"
            f"    {latest_release:<20}: {stable_v or 'unknown'}\n"
            f"    upstream latest   : {_format_upstream(upstream_info)}"
        )
    return "\n".join(lines)


@workflow.defn
class NixpkgsRadarWorkflow:
    @workflow.run
    async def run(self, github_handle: str) -> str:
        await workflow.execute_activity(
            clone_or_fetch_nixpkgs,
            start_to_close_timeout=_CLONE_TIMEOUT,
            heartbeat_timeout=_HEARTBEAT_TIMEOUT,
            retry_policy=_CLONE_RETRY,
        )

        latest_release, packages = await asyncio.gather(
            workflow.execute_activity(
                find_latest_nixos_release,
                start_to_close_timeout=_FAST_TIMEOUT,
                retry_policy=_FAST_RETRY,
            ),
            workflow.execute_activity(
                find_maintained_packages,
                args=[github_handle],
                start_to_close_timeout=_EVAL_TIMEOUT,
                heartbeat_timeout=_HEARTBEAT_TIMEOUT,
                retry_policy=_EVAL_RETRY,
            ),
        )

        if not packages:
            return f"No nixpkgs packages found for GitHub handle '{github_handle}'."

        stable_ref = f"refs/remotes/origin/{latest_release}"
        branch_refs = [ref for _, ref in _BRANCHES] + [stable_ref]

        sem = asyncio.Semaphore(_PACKAGE_CONCURRENCY)

        async def _fetch_one(pkg: dict) -> tuple:
            file_path = pkg.get("file") or None
            async with sem:
                return await asyncio.gather(
                    *[
                        workflow.execute_activity(
                            get_version_at_ref,
                            args=[pkg["attr"], ref, file_path],
                            start_to_close_timeout=_FAST_TIMEOUT,
                            retry_policy=_FAST_RETRY,
                        )
                        for ref in branch_refs
                    ],
                    workflow.execute_activity(
                        fetch_upstream_version,
                        args=[pkg["attr"], pkg.get("homepage", ""), file_path],
                        start_to_close_timeout=_UPSTREAM_TIMEOUT,
                        retry_policy=_UPSTREAM_RETRY,
                    ),
                )

        all_versions = await asyncio.gather(*[_fetch_one(pkg) for pkg in packages])

        packages, all_versions = zip(
            *sorted(zip(packages, all_versions), key=lambda pair: pair[0]["attr"])
        )
        all_versions = _propagate_versions(list(packages), list(all_versions))

        summary_text = _format_packages(packages, all_versions, latest_release)
        header = (
            f"GitHub handle: {github_handle}\n"
            f"Latest stable branch: {latest_release}\n"
            f"Package count: {len(packages)}\n\n"
        )

        agent = Agent(
            name="nixpkgs Release Radar Assistant",
            instructions=_SYSTEM_PROMPT,
            model=_MODEL,
        )
        result = await Runner.run(
            agent,
            input=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": header + summary_text,
                        }
                    ],
                }
            ],
        )
        return result.final_output