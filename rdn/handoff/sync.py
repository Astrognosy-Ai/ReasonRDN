"""
rdn/handoff/sync.py -- Repo-state sync runner for ReasonRDN (coherent).

Scans git repositories, builds rich state snapshots, deposits them as handoff artifacts
with artifact metadata so agents can later resolve "what was the state when X happened".

Used by:
- Desktop GUI (ReasonRDN console)
- Git hooks (post-commit, post-checkout, post-merge)
- CLI: rdn-sync (console script)
- Direct python -m rdn.handoff.sync
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from threading import Event
from typing import Any, Dict, Iterable, List, Optional, Set

from rdn.handoff.protocol import ReasonRDN

DEFAULT_ROOT = os.environ.get("RDN_REPO_ROOT", r"C:\Users\Scooter\Coworker\Projects\GitHub")
DEFAULT_INTERVAL = int(os.environ.get("RDN_SCAN_INTERVAL", "300"))
DEFAULT_NODE_URL = os.environ.get("RDN_NODE_URL") or os.environ.get("WARF_NODE_URL") or os.environ.get("REASON_NODE_URL")
# For optional WARF networking, prefer REASON_USE_NETWORK=1. Legacy Xchange names remain compatible.
DEFAULT_TAGS = ["handoff", "ReasonRDN", "repo-state", "auto"]

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "__pycache__",
    ".copilot",
    ".pytest_cache",
}

HOOK_MARKER = "# RDN AUTO-SYNC"
HOOK_BODY = f"""#!/bin/sh
{HOOK_MARKER}
RDN_PYTHON="${{RDN_PYTHON:-python}}"
"$RDN_PYTHON" -m rdn.handoff.sync --repo "$PWD" --once >/dev/null 2>&1
"""

logger = logging.getLogger("rdn.sync")


def run_git(repo_path: str, args: Iterable[str]) -> str:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(
        ["git", *args],
        cwd=repo_path,
        capture_output=True,
        text=True,
        creationflags=creationflags,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git command failed")
    return result.stdout.strip()


def find_git_repos(root: str) -> List[str]:
    repos: List[str] = []
    for dirpath, dirnames, _ in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        git_dir = os.path.join(dirpath, ".git")
        if os.path.isdir(git_dir) or os.path.isfile(git_dir):
            repos.append(dirpath)
            dirnames[:] = []
    return repos


def parse_log_lines(lines: str) -> List[Dict[str, str]]:
    commits: List[Dict[str, str]] = []
    for line in lines.splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3:
            commits.append({
                "hash": parts[0],
                "date": parts[1],
                "subject": parts[2],
            })
    return commits


def split_lines(value: str) -> List[str]:
    return [line for line in value.splitlines() if line.strip()]


def collect_repo_state(repo_path: str) -> Dict[str, Any]:
    state: Dict[str, Any] = {
        "repo_path": repo_path,
        "repo_name": os.path.basename(repo_path.rstrip("\\/")),
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        branch = run_git(repo_path, ["rev-parse", "--abbrev-ref", "HEAD"])
        head_line = run_git(repo_path, ["log", "-1", "--pretty=format:%H|%s|%cd", "--date=iso"])
        status_raw = run_git(repo_path, ["status", "--porcelain=v1", "-b"])
        diff_stat = run_git(repo_path, ["diff", "--stat"])
        staged_stat = run_git(repo_path, ["diff", "--cached", "--stat"])
        last_commits_raw = run_git(repo_path, ["log", "-5", "--pretty=format:%h|%ad|%s", "--date=iso"])
        commit_files_raw = run_git(repo_path, ["log", "-5", "--name-only", "--pretty=format:"])
        changed_files_raw = run_git(repo_path, ["diff", "--name-only"])
        staged_files_raw = run_git(repo_path, ["diff", "--cached", "--name-only"])
    except RuntimeError as exc:
        state["error"] = str(exc)
        return state

    status_lines = split_lines(status_raw)
    commit_files = split_lines(commit_files_raw)
    changed_files = split_lines(changed_files_raw)
    staged_files = split_lines(staged_files_raw)

    touched_files: Set[str] = set(commit_files + changed_files + staged_files)
    touched_dirs: Set[str] = set()
    for file_path in touched_files:
        top_dir = file_path.split("/")[0] if "/" in file_path else os.path.dirname(file_path)
        if top_dir:
            touched_dirs.add(top_dir)

    head_parts = head_line.split("|", 2)
    head = {
        "hash": head_parts[0] if len(head_parts) > 0 else "",
        "subject": head_parts[1] if len(head_parts) > 1 else "",
        "date": head_parts[2] if len(head_parts) > 2 else "",
    }

    state.update({
        "branch": branch,
        "head": head,
        "status": status_lines,
        "diff_stat": split_lines(diff_stat),
        "staged_stat": split_lines(staged_stat),
        "last_commits": parse_log_lines(last_commits_raw),
        "touched_files": sorted(touched_files),
        "touched_dirs": sorted(touched_dirs),
        "dirty": any(line for line in status_lines if not line.startswith("##")),
    })

    return state


def build_tokens(state: Dict[str, Any]) -> List[str]:
    tokens: List[str] = []
    for key in ("repo_name", "branch"):
        value = state.get(key)
        if value:
            tokens.append(str(value))

    head = state.get("head", {})
    if isinstance(head, dict):
        for key in ("hash", "subject", "date"):
            if head.get(key):
                tokens.append(str(head[key]))

    for line in state.get("status", []):
        tokens.extend(line.split())

    for path in state.get("touched_files", []):
        tokens.append(path)

    for commit in state.get("last_commits", []):
        if isinstance(commit, dict):
            tokens.extend([commit.get("hash", ""), commit.get("subject", "")])

    return [t for t in tokens if t]


def format_summary(state: Dict[str, Any]) -> str:
    return json.dumps(state, ensure_ascii=True, indent=2)


def deposit_state(rdn: ReasonRDN, state: Dict[str, Any]) -> Dict[str, Any]:
    project = state.get("repo_name", "unknown")
    summary = format_summary(state)
    tokens = build_tokens(state) or [project]
    return rdn.deposit_handoff(project, summary, tokens, tags=DEFAULT_TAGS)


def install_hooks(repo_path: str) -> List[str]:
    hooks_dir = os.path.join(repo_path, ".git", "hooks")
    if not os.path.isdir(hooks_dir):
        return []

    installed = []
    for hook in ("post-commit", "post-checkout", "post-merge"):
        hook_path = os.path.join(hooks_dir, hook)
        existing = ""
        if os.path.exists(hook_path):
            try:
                with open(hook_path, "r", encoding="utf-8") as handle:
                    existing = handle.read()
            except OSError:
                continue
            if HOOK_MARKER in existing:
                continue

        try:
            with open(hook_path, "w", encoding="utf-8", newline="\n") as handle:
                if existing:
                    if not existing.endswith("\n"):
                        existing += "\n"
                    handle.write(existing)
                    handle.write("\n")
                    handle.write(HOOK_BODY)
                else:
                    handle.write(HOOK_BODY)
            try:
                os.chmod(hook_path, os.stat(hook_path).st_mode | 0o111)
            except OSError:
                pass
            installed.append(hook_path)
        except OSError:
            continue

    return installed


def scan_repos(root: str, install_repo_hooks: bool) -> Dict[str, Any]:
    repos = find_git_repos(root)
    results: List[Dict[str, Any]] = []
    hooks_installed = 0
    errors: List[Dict[str, str]] = []

    for repo in repos:
        try:
            if install_repo_hooks:
                hooks_installed += len(install_hooks(repo))
            state = collect_repo_state(repo)
            if "error" in state:
                errors.append({"repo": repo, "error": str(state["error"])})
            results.append(state)
        except Exception as exc:
            errors.append({"repo": repo, "error": str(exc)})

    return {
        "repos": repos,
        "states": results,
        "hooks_installed": hooks_installed,
        "errors": errors,
    }


def run_once(repo: Optional[str], root: str, node_url: Optional[str], install_repo_hooks: bool) -> Dict[str, Any]:
    rdn = ReasonRDN(node_url=node_url)
    summary = {
        "repos_scanned": 0,
        "deposits_attempted": 0,
        "deposits_succeeded": 0,
        "hooks_installed": 0,
        "errors": [],
    }

    if repo:
        summary["repos_scanned"] = 1
        if install_repo_hooks:
            summary["hooks_installed"] = len(install_hooks(repo))
        state = collect_repo_state(repo)
        summary["deposits_attempted"] = 1
        if "error" in state:
            summary["errors"].append({"repo": repo, "error": str(state["error"])})
            return summary
        result = deposit_state(rdn, state)
        if result.get("status") == "remembered":
            summary["deposits_succeeded"] = 1
        else:
            summary["errors"].append({"repo": repo, "error": str(result.get("message", "deposit failed"))})
        return summary

    scan_result = scan_repos(root, install_repo_hooks)
    states = scan_result["states"]
    summary["repos_scanned"] = len(scan_result["repos"])
    summary["hooks_installed"] = scan_result["hooks_installed"]
    summary["errors"].extend(scan_result["errors"])

    for state in states:
        summary["deposits_attempted"] += 1
        repo_name = state.get("repo_path", state.get("repo_name", "unknown"))
        if "error" in state:
            summary["errors"].append({"repo": str(repo_name), "error": str(state["error"])})
            continue
        result = deposit_state(rdn, state)
        if result.get("status") == "remembered":
            summary["deposits_succeeded"] += 1
        else:
            summary["errors"].append({"repo": str(repo_name), "error": str(result.get("message", "deposit failed"))})

    logger.info(
        "sync complete repos=%s succeeded=%s/%s hooks=%s errors=%s",
        summary["repos_scanned"],
        summary["deposits_succeeded"],
        summary["deposits_attempted"],
        summary["hooks_installed"],
        len(summary["errors"]),
    )
    return summary


def run_loop(
    root: str,
    interval: int,
    node_url: Optional[str],
    install_repo_hooks: bool,
    stop_event: Optional[Event] = None,
) -> None:
    while True:
        run_once(None, root, node_url, install_repo_hooks)
        if stop_event is not None:
            if stop_event.wait(interval):
                break
        else:
            time.sleep(interval)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ReasonRDN repo-state sync runner")
    parser.add_argument("--root", default=DEFAULT_ROOT, help="Root directory to scan for repos")
    parser.add_argument("--repo", help="Single repo path to sync")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help="Seconds between scans")
    parser.add_argument("--node-url", default=DEFAULT_NODE_URL, help="WARF node base URL")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument("--install-hooks", action="store_true", help="Install git hooks in discovered repos")
    return parser


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = build_parser()
    args = parser.parse_args()

    if args.once:
        run_once(args.repo, args.root, args.node_url, args.install_hooks)
        return

    run_loop(args.root, args.interval, args.node_url, args.install_hooks)


if __name__ == "__main__":
    main()
