"""Refuse a commit that adds a token this project handles (pre-commit hook `no-secrets`).

`detect-private-key` catches only PEM keys. This scan matches the token shapes the
project uses: Slack bot and app tokens, OpenRouter and Anthropic API keys. It prints the
file, the line and the kind of token, never the token. CI runs gitleaks as the broad net.

Usage: check_secrets.py PATH [PATH ...]   (prek passes the staged files)
"""

import re
import sys
from pathlib import Path

PATTERNS = {
    "Slack bot token": re.compile(r"xox[abposr]-[0-9A-Za-z-]{20,}"),
    "Slack app token": re.compile(r"xapp-\d-[0-9A-Za-z-]{20,}"),
    "OpenRouter key": re.compile(r"sk-or-v1-[0-9a-f]{32,}"),
    "Anthropic key": re.compile(r"sk-ant-[0-9A-Za-z_-]{20,}"),
}


def findings(path: Path) -> list[str]:
    if not path.is_file():  # a deleted or renamed file in the staged list
        return []
    text = path.read_bytes().decode("utf-8", errors="ignore")
    found = []
    for number, line in enumerate(text.splitlines(), start=1):
        for kind, pattern in PATTERNS.items():
            if pattern.search(line):
                found.append(f"{path}:{number}: {kind}")
    return found


def main(paths: list[str]) -> int:
    found = [f for p in paths for f in findings(Path(p))]
    if found:
        print("\n".join(found))
        print(
            "\nA token is staged. Remove it, keep tokens in .env (gitignored), and rotate it"
            " if it was ever pushed."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
