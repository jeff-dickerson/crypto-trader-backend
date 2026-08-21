#!/usr/bin/env python3
"""Fails if an em dash (U+2014) appears anywhere in tracked source or docs.

Conduct rule 1 (see AGENTS.md): no em dashes anywhere produced for this
project, in code, comments, docs, or commit messages.
This script covers the code-and-docs half, machine-checkable in CI or a
pre-commit hook.
Commit-message wording is a manual-review item; see AGENTS.md.
"""

from __future__ import annotations

import subprocess
import sys

EM_DASH = "—"


def tracked_files() -> list[str]:
    output = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, check=True
    ).stdout
    return [line for line in output.splitlines() if line]


def main() -> int:
    violations: list[str] = []
    for path in tracked_files():
        # Skip this script itself: it necessarily contains the character it is
        # checking for, inside a string literal and this docstring's prose.
        if path == "scripts/check_no_em_dash.py":
            continue
        try:
            with open(path, encoding="utf-8") as f:
                for line_number, line in enumerate(f, start=1):
                    if EM_DASH in line:
                        violations.append(f"{path}:{line_number}: {line.strip()}")
        except (UnicodeDecodeError, IsADirectoryError, OSError):
            continue

    if violations:
        print("Em dash (U+2014) found in tracked files, not allowed:")
        for violation in violations:
            print(f"  {violation}")
        return 1

    print("No em dashes found in tracked files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
