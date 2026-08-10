#!/usr/bin/env python3
"""Build a deterministic allowlist release archive, SPDX SBOM, and checksums."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "codex-openrouter",
    "LICENSE",
    "README.md",
    "README.en.md",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "THIRD_PARTY_NOTICES.md",
    "RELEASE_NOTES.md",
)
DIRECTORIES = ("adapters", "models", "portable", "profiles", "src", "tests", "scripts")
EXCLUDED_PARTS = {"node_modules", "__pycache__", ".generated", ".test-output", "dist"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_epoch() -> int:
    raw = os.environ.get("SOURCE_DATE_EPOCH")
    if raw:
        return int(raw)
    result = subprocess.run(
        ["git", "-C", str(ROOT), "log", "-1", "--format=%ct"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return int(result.stdout.strip()) if result.returncode == 0 and result.stdout.strip() else 0


def copy_allowlist(destination: Path) -> None:
    for name in FILES:
        source = ROOT / name
        if not source.is_file() or source.is_symlink():
            raise RuntimeError(f"required release file is missing or a symlink: {source}")
        shutil.copy2(source, destination / name)
    for name in DIRECTORIES:
        source = ROOT / name
        target = destination / name
        target.mkdir()
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(source)
            if any(part in EXCLUDED_PARTS for part in relative.parts):
                continue
            if path.is_symlink():
                raise RuntimeError(f"release allowlist refuses symlink: {path}")
            output = target / relative
            if path.is_dir():
                output.mkdir(exist_ok=True)
            elif path.is_file():
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, output)


def deterministic_tar(source: Path, target: Path, epoch: int) -> None:
    with target.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=epoch) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for path in [source, *sorted(source.rglob("*"))]:
                    relative = path.relative_to(source.parent)
                    info = archive.gettarinfo(str(path), arcname=str(relative))
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    info.mtime = epoch
                    if info.isdir():
                        info.mode = 0o755
                        archive.addfile(info)
                    else:
                        info.mode = 0o755 if os.access(path, os.X_OK) else 0o644
                        with path.open("rb") as handle:
                            archive.addfile(info, handle)


def spdx_id(value: str) -> str:
    return "SPDXRef-" + hashlib.sha256(value.encode()).hexdigest()[:24]


def build_sbom(source: Path, version: str, namespace_hash: str, epoch: int) -> dict:
    files = []
    relationships = []
    package_id = "SPDXRef-Package-codex-openrouter-desktop"
    for path in sorted(item for item in source.rglob("*") if item.is_file()):
        relative = str(path.relative_to(source))
        file_id = spdx_id(relative)
        files.append(
            {
                "SPDXID": file_id,
                "fileName": f"./{relative}",
                "checksums": [{"algorithm": "SHA256", "checksumValue": sha256(path)}],
                "licenseConcluded": "NOASSERTION",
                "copyrightText": "NOASSERTION",
            }
        )
        relationships.append(
            {"spdxElementId": package_id, "relationshipType": "CONTAINS", "relatedSpdxElement": file_id}
        )
    packages = [
        {
            "name": "codex-openrouter-desktop",
            "SPDXID": package_id,
            "versionInfo": version,
            "downloadLocation": "https://github.com/hirorylabo/codex-openrouter-desktop",
            "filesAnalyzed": True,
            "licenseConcluded": "MIT",
            "licenseDeclared": "MIT",
            "copyrightText": "Copyright (c) 2026 hirorylabo",
        }
    ]
    lock = json.loads((source / "portable/patcher-js/package-lock.json").read_text(encoding="utf-8"))
    for package_path, metadata in sorted(lock.get("packages", {}).items()):
        if not package_path.startswith("node_modules/"):
            continue
        name = package_path.removeprefix("node_modules/")
        package_spdx = spdx_id(f"npm:{name}@{metadata.get('version', '')}")
        packages.append(
            {
                "name": name,
                "SPDXID": package_spdx,
                "versionInfo": str(metadata.get("version", "")),
                "downloadLocation": str(metadata.get("resolved", "NOASSERTION")),
                "filesAnalyzed": False,
                "licenseConcluded": "NOASSERTION",
                "licenseDeclared": str(metadata.get("license", "NOASSERTION")),
                "copyrightText": "NOASSERTION",
            }
        )
        relationships.append(
            {"spdxElementId": package_id, "relationshipType": "DEPENDS_ON", "relatedSpdxElement": package_spdx}
        )
    return {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"codex-openrouter-desktop-{version}",
        "documentNamespace": f"https://github.com/hirorylabo/codex-openrouter-desktop/sbom/{version}/{namespace_hash}",
        "creationInfo": {
            "created": datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "creators": ["Tool: scripts/build_release.py"],
        },
        "packages": packages,
        "files": files,
        "relationships": [
            {"spdxElementId": "SPDXRef-DOCUMENT", "relationshipType": "DESCRIBES", "relatedSpdxElement": package_id},
            *relationships,
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("version", help="v-prefixed release version, for example v0.1.0")
    parser.add_argument("--dist", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    if args.version != "v0.1.0":
        raise RuntimeError("v0.1.0 is the only release version supported by this source tree")
    dist = args.dist.resolve()
    dist.mkdir(parents=True, exist_ok=True)
    archive = dist / f"codex-openrouter-desktop-{args.version}.tar.gz"
    sbom = dist / f"codex-openrouter-desktop-{args.version}.spdx.json"
    epoch = source_epoch()
    with tempfile.TemporaryDirectory(prefix="codex-openrouter-release-") as temporary:
        release_root = Path(temporary) / f"codex-openrouter-desktop-{args.version}"
        release_root.mkdir()
        copy_allowlist(release_root)
        deterministic_tar(release_root, archive, epoch)
        document = build_sbom(release_root, args.version, sha256(archive), epoch)
        sbom.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    checksums = dist / "SHA256SUMS"
    checksums.write_text(
        "".join(f"{sha256(path)}  {path.name}\n" for path in (archive, sbom)),
        encoding="utf-8",
    )
    print(archive)
    print(sbom)
    print(checksums)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
