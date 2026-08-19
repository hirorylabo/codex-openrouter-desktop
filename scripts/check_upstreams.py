#!/usr/bin/env python3
"""Pinned upstreamのrelease・HEAD・参照file hashを報告する週次CI。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "models/upstreams.json"


class UpstreamError(RuntimeError):
    pass


def load_config() -> dict:
    document = json.loads(CONFIG.read_text(encoding="utf-8"))
    if document.get("schema_version") != 1 or not isinstance(document.get("upstreams"), list):
        raise UpstreamError("upstreams.jsonのschemaが不正です")
    for upstream in document["upstreams"]:
        if not isinstance(upstream, dict):
            raise UpstreamError("upstream entryがobjectではありません")
        for key in ("name", "repository", "branch", "commit", "files"):
            if key not in upstream:
                raise UpstreamError(f"{upstream.get('name', '?')} に {key} がありません")
        if len(upstream["commit"]) != 40 or not isinstance(upstream["files"], list):
            raise UpstreamError(f"{upstream['name']} のpinが不正です")
        for item in upstream["files"]:
            if set(item) != {"path", "sha256"} or len(item["sha256"]) != 64:
                raise UpstreamError(f"{upstream['name']} のfile pinが不正です")
    return document


def fetch_json(url: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "codex-openrouter-upstream-watch",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        document = json.load(response)
    if not isinstance(document, dict):
        raise UpstreamError(f"GitHub API responseがobjectではありません: {url}")
    return document


def fetch_bytes(url: str) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": "codex-openrouter-upstream-watch"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def check(document: dict) -> tuple[list[str], bool]:
    lines = ["# Upstream watch", ""]
    drift = False
    for upstream in document["upstreams"]:
        name = upstream["name"]
        repository = upstream["repository"]
        branch = upstream["branch"]
        head = fetch_json(
            f"https://api.github.com/repos/{repository}/commits/{branch}"
        ).get("sha")
        lines.append(f"## {name}")
        lines.append(f"- pinned commit: `{upstream['commit']}`")
        lines.append(f"- current commit: `{head}`")
        if head != upstream["commit"]:
            drift = True

        expected_release = upstream.get("release")
        if expected_release:
            latest = fetch_json(
                f"https://api.github.com/repos/{repository}/releases/latest"
            ).get("tag_name")
            lines.append(f"- pinned/latest release: `{expected_release}` / `{latest}`")
            if latest != expected_release:
                drift = True

        changed_files: list[str] = []
        if isinstance(head, str) and len(head) == 40:
            for item in upstream["files"]:
                raw = fetch_bytes(
                    f"https://raw.githubusercontent.com/{repository}/{head}/{item['path']}"
                )
                actual = hashlib.sha256(raw).hexdigest()
                lines.append(f"- `{item['path']}`: `{actual}`")
                if actual != item["sha256"]:
                    changed_files.append(item["path"])
                    drift = True
        if changed_files:
            lines.append(
                "- **ACTION REQUIRED:** tool wire fixtureを再生成し、実機canary後にpinを更新: "
                + ", ".join(f"`{path}`" for path in changed_files)
            )
        elif head != upstream["commit"]:
            lines.append("- 参照fileは不変。commit差分を確認してpin更新可否を判断してください。")
        lines.append("")
    return lines, drift


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    try:
        document = load_config()
        if args.validate_only:
            print("UPSTREAM CONFIG: PASS")
            return 0
        lines, drift = check(document)
    except (OSError, ValueError, urllib.error.URLError, UpstreamError) as exc:
        print(f"UPSTREAM WATCH: ERROR: {exc}")
        return 2
    report = "\n".join(lines) + "\n"
    print(report, end="")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as handle:
            handle.write(report)
    return 1 if drift else 0


if __name__ == "__main__":
    raise SystemExit(main())
