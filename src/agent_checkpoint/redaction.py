import re

SECRET_PATTERNS = (
    (
        "github-token",
        re.compile(r"\b(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,})\b"),
    ),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("openai-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("google-api-key", re.compile(r"\bAIza[A-Za-z0-9_-]{30,}\b")),
    (
        "private-key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
        ),
    ),
    (
        "bearer-token",
        re.compile(r"(?i)(Authorization\s*:\s*Bearer\s+)[A-Za-z0-9._~+/-]{24,}"),
    ),
    ("cookie", re.compile(r"(?i)(Cookie\s*:\s*)[^\s;=]+=[^\s;]{20,}")),
    ("url-credential", re.compile(r"\bhttps?://[^\s/:@]{3,}:[^\s/@]{8,}@[^\s/]+")),
    (
        "assigned-secret",
        re.compile(
            r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY|APIKEY|OBSIDIAN_KEY)\s*[=:]\s*)"
            r"[A-Za-z0-9._~+/-]{24,}"
        ),
    ),
)


def redact_secrets(text: str) -> tuple[str, list[str]]:
    result = text or ""
    matches: list[str] = []
    for name, pattern in SECRET_PATTERNS:
        if pattern.search(result):
            matches.append(name)
            if name in {"bearer-token", "assigned-secret"}:
                result = pattern.sub(lambda m: f"{m.group(1)}[REDACTED:{name}]", result)
            else:
                result = pattern.sub(f"[REDACTED:{name}]", result)
    return result, matches


def find_secrets(text: str) -> list[str]:
    return redact_secrets(text)[1]
