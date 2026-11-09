"""Refuse to store anything that looks like a credential.

Every write path (CLI, MCP, hooks, the live view) goes through the Store, which calls
`check()`. A match raises, and the message tells the caller to save the text without it.
"""

from __future__ import annotations

import re

from tether.errors import TetherError

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("an Anthropic/OpenAI-style API key", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{20,}")),
    ("a GitHub token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}")),
    ("an AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("a Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("a Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("a Stripe key", re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("a JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("a private key", re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")),
    ("a password/token assignment",
     re.compile(r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)"
                r"\s*[:=]\s*['\"]?(?!\s)[^\s'\"]{8,}")),
]


class SecretFound(TetherError):
    """Raised when text looks like a credential."""


def find(text: str) -> str | None:
    """Name of the first credential pattern matched, or None."""
    for name, pat in PATTERNS:
        if pat.search(text):
            return name
    return None


def check(*texts: str | None) -> None:
    for text in texts:
        if not text:
            continue
        hit = find(text)
        if hit:
            raise SecretFound(
                f"refused: this looks like {hit}. Tether never stores secrets. "
                "Save the text again with the secret removed or replaced by a placeholder.")
