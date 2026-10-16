import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


def run_git(cwd: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.rstrip("\n") if result.returncode == 0 else ""


def find_repo_root(cwd: Path) -> Path:
    root = run_git(cwd, "rev-parse", "--show-toplevel")
    return Path(root).resolve() if root else cwd.resolve()


def git_snapshot(cwd: Path) -> dict[str, Any]:
    root = find_repo_root(cwd)
    status = run_git(root, "status", "--short")
    return {
        "repo": str(root),
        "branch": run_git(root, "branch", "--show-current"),
        "head": run_git(root, "rev-parse", "HEAD"),
        "changed_files": [line[3:] for line in status.splitlines() if len(line) > 3],
        "dirty": bool(status),
    }


def _normalize_remote(url: str) -> str:
    if "://" in url:
        parts = urlsplit(url)
        url = urlunsplit(
            (parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", "")
        )
    url = re.sub(r"^[a-z+]+://([^@/]+@)?", "", url.strip())
    url = re.sub(r"^[^@/]+@([^:/]+):", r"\1/", url)
    return url.removesuffix(".git").rstrip("/").lower()


def repo_identity(cwd: Path) -> dict[str, str]:
    root = find_repo_root(cwd)
    common = run_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return {
        "repo": str(root),
        "git_common_dir": str(Path(common).resolve()) if common else "",
        "remote": _normalize_remote(
            run_git(root, "config", "--get", "remote.origin.url")
        ),
    }


def same_repo(record: dict[str, Any], current: dict[str, str]) -> bool:
    """Match by path, shared git dir (worktrees), or origin remote (clones)."""
    if record.get("repo") == current["repo"]:
        return True
    identity = record
    if "git_common_dir" not in record and Path(str(record.get("repo", ""))).exists():
        identity = repo_identity(Path(str(record["repo"])))
    for key in ("git_common_dir", "remote"):
        if identity.get(key) and identity.get(key) == current.get(key):
            return True
    return False
