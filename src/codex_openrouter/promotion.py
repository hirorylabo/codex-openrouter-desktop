from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Callable, Iterable

from .app import write_json


class PromotionError(RuntimeError):
    pass


def _copy(source: Path, target: Path) -> None:
    if source.is_symlink() or not source.exists():
        raise PromotionError(f"staging sourceが不正です: {source}")
    if source.is_dir():
        for path in source.rglob("*"):
            if path.is_symlink() and not path.resolve().is_relative_to(source.resolve()):
                raise PromotionError(f"staging directory外を指すsymlinkがあります: {path}")
        if source.suffix == ".app" and Path("/usr/bin/ditto").is_file():
            subprocess.run(["/usr/bin/ditto", str(source), str(target)], check=True)
        else:
            shutil.copytree(source, target, copy_function=shutil.copy2, symlinks=True)
    else:
        shutil.copy2(source, target)


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def atomic_promote(
    replacements: Iterable[tuple[Path | None, Path]],
    backup_root: Path,
    verify: Callable[[], None],
) -> None:
    items = list(replacements)
    if not items or len({target for _source, target in items}) != len(items):
        raise PromotionError("promotion targetが空または重複しています")
    if backup_root.exists() or backup_root.is_symlink():
        raise PromotionError(f"backup targetが既に存在します: {backup_root}")
    backup_root.mkdir(parents=True, mode=0o700)
    originals = backup_root / "originals"
    failed = backup_root / "failed-new"
    originals.mkdir(mode=0o700)
    failed.mkdir(mode=0o700)
    prepared: list[tuple[Path | None, Path, Path | None, Path | None]] = []
    report: dict = {"schema_version": 1, "result": "preparing", "targets": []}
    try:
        for index, (source, target) in enumerate(items):
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink():
                raise PromotionError(f"symlink targetは置換しません: {target}")
            incoming = target.parent / f".{target.name}.upgrade-new" if source is not None else None
            if incoming is not None:
                if incoming.exists() or incoming.is_symlink():
                    raise PromotionError(f"stale staging targetがあります: {incoming}")
                _copy(source, incoming)
            elif not target.exists():
                raise PromotionError(f"削除対象が存在しません: {target}")
            original = originals / str(index) if target.exists() else None
            adjacent_original = None
            if original is not None and target.suffix == ".app":
                # provenance付きappを別directoryへrenameするとmacOSで停止しうる。
                # backupはcopyで確保し、切替renameは同じdirectory内だけにする。
                _copy(target, original)
                adjacent_original = target.parent / f".{target.name}.upgrade-old"
                if adjacent_original.exists() or adjacent_original.is_symlink():
                    raise PromotionError(f"stale app backupがあります: {adjacent_original}")
            prepared.append((incoming, target, original, adjacent_original))
            report["targets"].append(
                {
                    "target": str(target),
                    "had_original": original is not None,
                    "remove": source is None,
                }
            )

        report["result"] = "switching"
        write_json(backup_root / "promotion.json", report)
        applied: list[tuple[Path, Path | None, Path | None]] = []
        for incoming, target, original, adjacent_original in prepared:
            if original is not None:
                os.replace(target, adjacent_original or original)
            if incoming is not None:
                try:
                    os.replace(incoming, target)
                except Exception:
                    restore = adjacent_original or original
                    if restore is not None and restore.exists():
                        os.replace(restore, target)
                    raise
            applied.append((target, original, adjacent_original))

        verify()
        for _target, _original, adjacent_original in applied:
            if adjacent_original is not None and adjacent_original.exists():
                _remove(adjacent_original)
        report["result"] = "promoted-and-verified"
        write_json(backup_root / "promotion.json", report)
    except Exception as error:
        for index, (target, original, adjacent_original) in reversed(
            list(enumerate(locals().get("applied", [])))
        ):
            if target.exists() and not target.is_symlink():
                if target.suffix == ".app":
                    _copy(target, failed / str(index))
                    _remove(target)
                else:
                    os.replace(target, failed / str(index))
            if adjacent_original is not None and adjacent_original.exists():
                os.replace(adjacent_original, target)
            elif original is not None and original.exists():
                if target.suffix == ".app":
                    _copy(original, target)
                else:
                    os.replace(original, target)
        for index, (incoming, target, _original, _adjacent_original) in enumerate(prepared):
            if incoming is not None and (incoming.exists() or incoming.is_symlink()):
                if target.suffix == ".app":
                    _copy(incoming, failed / f"prepared-{index}")
                    _remove(incoming)
                else:
                    os.replace(incoming, failed / f"prepared-{index}")
        report["result"] = "failed-auto-rolled-back"
        report["error"] = f"{type(error).__name__}: {error}"
        write_json(backup_root / "promotion.json", report)
        raise PromotionError(f"promotionに失敗し、自動rollbackしました: {error}") from error


def rollback_replacements(backup_root: Path) -> list[tuple[Path | None, Path]]:
    report_path = backup_root / "promotion.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PromotionError(f"promotion backupを読めません: {error}") from error
    if report.get("result") != "promoted-and-verified":
        raise PromotionError("成功済みpromotion backupではありません")
    targets = report.get("targets")
    if not isinstance(targets, list) or not targets:
        raise PromotionError("promotion backup inventoryが不正です")
    replacements: list[tuple[Path | None, Path]] = []
    for index, item in enumerate(targets):
        if not isinstance(item, dict) or not isinstance(item.get("target"), str):
            raise PromotionError("promotion backup targetが不正です")
        target = Path(item["target"])
        if not target.is_absolute():
            raise PromotionError("promotion backup targetが絶対pathではありません")
        if item.get("had_original") is not True:
            replacements.append((None, target))
            continue
        source = backup_root / "originals" / str(index)
        if not source.exists() or source.is_symlink():
            raise PromotionError("promotion backup originalまたはtargetが不正です")
        replacements.append((source, target))
    return replacements
