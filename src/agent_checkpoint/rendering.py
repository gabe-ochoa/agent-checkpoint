from pathlib import Path
from typing import Any

from .core import STALE_CHECK_HOURS, stale_checks


def checkpoint_context(record: dict[str, Any], token_limit: int = 2000) -> str:
    max_chars = max(1000, token_limit * 4)
    lines = [
        f"Unfinished task {record.get('name') or record.get('id')}: {record.get('goal', '')}",
        f"Repository: {record.get('repo', '')}",
        f"Branch/HEAD: {record.get('branch', '')} {record.get('head', '')[:12]}",
    ]
    if record.get("diverged"):
        lines.append(
            "Warning: the current branch, HEAD, or checkout differs from the checkpoint."
        )
    if record.get("decisions"):
        lines.append("Decisions: " + "; ".join(record["decisions"]))
    if record.get("verification"):
        rendered = []
        for evidence in record["verification"]:
            if isinstance(evidence, dict):
                rendered.append(
                    f"{evidence.get('command', '')} [{evidence.get('status', 'pending')}]"
                )
            else:
                rendered.append(str(evidence))
        lines.append("Verification: " + "; ".join(rendered))
        if stale_checks(record):
            lines.append(
                f"Warning: {stale_checks(record)} check(s) pending for over {STALE_CHECK_HOURS}h."
            )
    if record.get("blocker"):
        lines.append("Blocker: " + record["blocker"])
    if record.get("blocker_history"):
        earlier = [item.get("blocker", "") for item in record["blocker_history"][-3:]]
        lines.append("Earlier blockers (resolved): " + "; ".join(earlier))
    lines.append("Next action: " + record.get("next_action", ""))
    return "\n".join(lines)[:max_chars]


def render_work_log(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return "No finished work in range."
    lines = ["# Work log", ""]
    for entry in entries:
        repo = Path(str(entry.get("repo", ""))).name
        lines.append(
            f"## {entry['finished_at'][:10]} {entry.get('name') or entry['id']} ({entry['outcome_status']})"
        )
        lines.append("")
        lines.append(
            f"- Repo: {repo} on {entry.get('branch') or '?'} at {str(entry.get('head', ''))[:8]}"
            f", started {str(entry.get('created_at', ''))[:10]}"
        )
        lines.append(f"- Goal: {' '.join(str(entry.get('goal', '')).split())}")
        if entry.get("outcome"):
            lines.append(f"- Outcome: {entry['outcome']}")
        if entry.get("artifacts"):
            lines.append(
                "- Artifacts: " + ", ".join(f"`{item}`" for item in entry["artifacts"])
            )
        if entry.get("decisions"):
            lines.append("- Decisions:")
            lines += [f"  - {decision}" for decision in entry["decisions"]]
        if entry.get("blockers_seen"):
            lines.append("- Blockers hit: " + "; ".join(entry["blockers_seen"]))
        checks = [
            item
            for item in entry.get("verification", [])
            if isinstance(item, dict)
            and item.get("status") in {"passed", "failed", "manual"}
        ]
        if checks:
            lines.append(
                "- Verification: "
                + "; ".join(
                    f"{item.get('command', '')} [{item.get('status')}]"
                    for item in checks[-5:]
                )
            )
        if entry.get("folded"):
            lines.append(
                f"- Also covers {len(entry['folded'])} superseded checkpoint(s)"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
