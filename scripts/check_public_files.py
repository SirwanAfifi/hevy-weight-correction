#!/usr/bin/env python3
"""Check the exact Git index contents before publishing this small repository.

This is a deliberately narrow guardrail, not a general-purpose secret scanner.
Review any allowlist expansion, and use synthetic data in all public examples.
Never print matched values: a failure could involve private account information.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys


PUBLIC_FILES = {
    ".gitignore",
    ".github/workflows/tests.yml",
    "LICENSE",
    "README.md",
    "config.example.json",
    "fix_hevy_weights.py",
    "scripts/check_public_files.py",
    "test_fix_hevy_weights.py",
}
REQUIRED_FILES = PUBLIC_FILES
MAX_PUBLIC_FILE_BYTES = 512 * 1024
FORBIDDEN_CONTENT = {
    "UUID (use clearly synthetic non-UUID identifiers in examples)": re.compile(
        rb"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I
    ),
    "local home directory": re.compile(rb"/(?:Users|home)/[^/\s\"']+"),
    "credential token": re.compile(
        rb"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16})\b"
    ),
    "private key": re.compile(rb"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
}
EXAMPLE_CONFIG = {
    "default_barbell_weight_kg": 20,
    "bar_weight_overrides": {},
    "default_is_private": None,
    "workout_privacy": {},
}


def git(repo: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout


def local_secrets(repo: Path) -> list[bytes]:
    """Include only known local key inputs; do not inspect other private files."""
    candidates = [os.environ.get("HEVY_API_KEY", "").strip().encode()]
    key_file = repo / ".hevy-api-key"
    if key_file.is_file():
        candidates.append(key_file.read_bytes().strip())
    return [value for value in candidates if len(value) >= 8]


def check_index(repo: Path) -> tuple[int, list[str]]:
    issues = []
    seen = set()
    secrets = local_secrets(repo)
    for record in git(repo, "ls-files", "--stage", "-z").split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode, oid, stage = metadata.decode("ascii").split()
        path = raw_path.decode("utf-8", errors="surrogateescape")
        seen.add(path)
        if path not in PUBLIC_FILES:
            # Do not echo an unapproved filename: it may itself contain private data.
            issues.append("A tracked path is not in PUBLIC_FILES; inspect git diff --cached locally.")
            continue
        if stage != "0":
            issues.append(f"{path}: unresolved index conflict")
            continue
        if mode not in {"100644", "100755"}:
            issues.append(f"{path}: only regular files may be published")
            continue
        # Read the staged blob, not the working copy that might already be sanitized.
        data = git(repo, "cat-file", "blob", oid)
        if len(data) > MAX_PUBLIC_FILE_BYTES:
            issues.append(f"{path}: unexpectedly large public file")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            issues.append(f"{path}: public files must be UTF-8 text")
        for label, pattern in FORBIDDEN_CONTENT.items():
            if pattern.search(data):
                issues.append(f"{path}: detected {label}")
        if any(value in data for value in secrets):
            issues.append(f"{path}: contains a local Hevy API key")
        if path == "config.example.json":
            try:
                config = json.loads(data)
            except (ValueError, UnicodeDecodeError):
                config = None
            if config != EXAMPLE_CONFIG:
                issues.append(f"{path}: must contain only the empty public example configuration")
    for path in sorted(REQUIRED_FILES - seen):
        issues.append(f"{path}: expected public file is not tracked")
    return len(seen), issues


def main() -> int:
    try:
        repo = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").decode().strip())
        count, issues = check_index(repo)
    except (OSError, subprocess.CalledProcessError, ValueError):
        print("Public-file check could not inspect the Git index. Run it inside the repository.", file=sys.stderr)
        return 1
    if issues:
        print("Public-file check failed:", file=sys.stderr)
        for issue in issues:
            print(f"- {issue}", file=sys.stderr)
        return 1
    print(f"Public-file check passed for {count} staged/tracked files. Review the diff before publishing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
