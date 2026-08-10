#!/usr/bin/env python3
"""Exercise upgrade promotion and automatic rollback without a real ChatGPT.app."""

from __future__ import annotations

from pathlib import Path
import tempfile

from codex_openrouter.promotion import PromotionError, atomic_promote, rollback_replacements


def write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="codex-openrouter-synthetic-e2e-") as temporary:
        root = Path(temporary)
        targets = [root / "live/support/VERSION", root / "live/home/install-manifest.json"]
        staged = [root / "stage/support/VERSION", root / "stage/home/install-manifest.json"]
        for path, value in zip(targets, ("0.1.0", "old-receipt"), strict=True):
            write(path, value)
        for path, value in zip(staged, ("0.1.1", "new-receipt"), strict=True):
            write(path, value)
        atomic_promote(list(zip(staged, targets, strict=True)), root / "backup-success", lambda: None)
        assert [path.read_text() for path in targets] == ["0.1.1", "new-receipt"]

        broken = [root / "broken/support/VERSION", root / "broken/home/install-manifest.json"]
        for path in broken:
            write(path, "broken")
        try:
            atomic_promote(
                list(zip(broken, targets, strict=True)),
                root / "backup-rollback",
                lambda: (_ for _ in ()).throw(RuntimeError("synthetic doctor failure")),
            )
        except PromotionError:
            pass
        else:
            raise AssertionError("synthetic failed verification did not roll back")
        assert [path.read_text() for path in targets] == ["0.1.1", "new-receipt"]
        assert len(list((root / "backup-rollback/failed-new").iterdir())) == 2
        atomic_promote(
            rollback_replacements(root / "backup-success"),
            root / "backup-manual-rollback",
            lambda: None,
        )
        assert [path.read_text() for path in targets] == ["0.1.0", "old-receipt"]
    print("SYNTHETIC E2E: upgrade promotion, automatic rollback, and manual rollback PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
