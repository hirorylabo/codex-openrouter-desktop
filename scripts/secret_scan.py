#!/usr/bin/env python3
"""Fail closed on packaged secrets, private paths, apps, ASAR, and runtime databases."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import tarfile


PATTERNS = {
    "OpenRouter API key": re.compile(rb"sk-or-(?:v1-)?[A-Za-z0-9_-]{24,}"),
    "OpenAI API key": re.compile(rb"sk-(?:proj-)?[A-Za-z0-9_-]{24,}"),
    "GitHub token": re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}"),
    "AWS access key": re.compile(rb"AKIA[0-9A-Z]{16}"),
    "private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "personal absolute path": re.compile(rb"/Users/(?:hk|mac)(?:/|\b)"),
}
FORBIDDEN_NAMES = {"auth.json", ".env", "Cookies", "Login Data"}
FORBIDDEN_SUFFIXES = {".asar", ".db", ".sqlite", ".sqlite3"}
# `.ruff_cache` はlint gateを回すたびに出来て、中に開発者のhome pathを持つ。
# 追跡外なので配布物へは入らないが、`--tree` はgitではなくfilesystemを歩くので
# ここで除外しないとローカル実行が必ず落ちる。
SKIP_PARTS = {".git", ".ruff_cache", "node_modules", "__pycache__", "dist"}
OS_JUNK_NAMES = {".DS_Store", ".Spotlight-V100", ".Trashes", ".fseventsd", "Thumbs.db", "desktop.ini"}


def scan_bytes(label: str, payload: bytes, findings: list[str]) -> None:
    for name, pattern in PATTERNS.items():
        if pattern.search(payload):
            findings.append(f"{label}: {name}")


def forbidden_path(path: Path | str) -> bool:
    value = Path(path)
    return (
        value.name in FORBIDDEN_NAMES
        or value.suffix.lower() in FORBIDDEN_SUFFIXES
        or any(part.endswith(".app") for part in value.parts)
    )


def os_junk_path(path: Path | str) -> bool:
    """OSが自動生成する配布対象外のファイル。

    作業ツリーには正当に存在しうるので`--tree`では検査せず、配布archiveでのみ弾く。
    `.DS_Store`はFinderの表示状態に加えて過去にそのディレクトリへ存在したファイル名を
    保持しうるため、成果物に載せない。
    """
    value = Path(path)
    return value.name in OS_JUNK_NAMES or value.name.startswith("._")


def scan_tree(root: Path, findings: list[str]) -> None:
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if any(part in SKIP_PARTS for part in relative.parts):
            continue
        if forbidden_path(relative):
            findings.append(f"tree:{relative}: forbidden runtime artifact")
        if path.is_file() and path.stat().st_size <= 32 * 1024 * 1024:
            scan_bytes(f"tree:{relative}", path.read_bytes(), findings)


def scan_git_history(root: Path, findings: list[str]) -> None:
    objects = subprocess.run(
        ["git", "-C", str(root), "rev-list", "--objects", "--all"],
        text=True,
        stdout=subprocess.PIPE,
        check=True,
    ).stdout.splitlines()
    for line in objects:
        object_id, _, name = line.partition(" ")
        if name and forbidden_path(name):
            findings.append(f"history:{name}: forbidden runtime artifact")
        size = subprocess.run(
            ["git", "-C", str(root), "cat-file", "-s", object_id],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if size.returncode != 0 or not size.stdout.strip().isdigit() or int(size.stdout) > 32 * 1024 * 1024:
            continue
        payload = subprocess.run(
            ["git", "-C", str(root), "cat-file", "-p", object_id],
            stdout=subprocess.PIPE,
            check=True,
        ).stdout
        scan_bytes(f"history:{object_id}:{name}", payload, findings)


def scan_archive(path: Path, findings: list[str]) -> None:
    with tarfile.open(path, "r:gz") as archive:
        for member in archive.getmembers():
            if forbidden_path(member.name):
                findings.append(f"archive:{member.name}: forbidden runtime artifact")
            if os_junk_path(member.name):
                findings.append(f"archive:{member.name}: OS-generated file must not ship")
            if member.isfile() and member.size <= 32 * 1024 * 1024:
                handle = archive.extractfile(member)
                if handle:
                    scan_bytes(f"archive:{member.name}", handle.read(), findings)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tree", type=Path)
    parser.add_argument("--git-history", action="store_true")
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    root = (args.tree or Path.cwd()).resolve()
    findings: list[str] = []
    scan_tree(root, findings)
    if args.git_history:
        scan_git_history(root, findings)
    if args.archive:
        scan_archive(args.archive.resolve(), findings)
    if findings:
        print("SECRET SCAN: FAIL")
        for finding in findings:
            print(f"- {finding}")
        return 1
    print("SECRET SCAN: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
