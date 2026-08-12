from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from codex_openrouter.processes import matching_processes
from codex_openrouter.promotion import PromotionError, atomic_promote, rollback_replacements
from scripts.build_release import validate_release_version


ROOT = Path(__file__).resolve().parents[1]


class VersionAndAdapterTests(unittest.TestCase):
    def test_release_tag_is_canonical_and_matches_version_file(self) -> None:
        expected = "v" + (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        self.assertEqual(expected, validate_release_version(expected))
        for invalid in ("0.1.1", "v0.01.1", "v0.1.0", "v1.2.3-rc1"):
            with self.subTest(invalid=invalid), self.assertRaises(RuntimeError):
                validate_release_version(invalid)


class ProcessTests(unittest.TestCase):
    def test_exact_executable_boundary_excludes_wrappers_and_prefixes(self) -> None:
        executable = Path("/Users/test/Applications/ChatGPT OpenRouter.app/Contents/MacOS/ChatGPT")
        table = "\n".join(
            (
                f"  101 {executable} --user-data-dir=/tmp/profile",
                f"  102 /bin/zsh -lc echo {executable}",
                f"  103 {executable}-helper --type=gpu",
                "not-a-pid ignored",
            )
        )
        self.assertEqual(
            [(101, f"{executable} --user-data-dir=/tmp/profile")],
            matching_processes(table, executable),
        )


class PromotionTests(unittest.TestCase):
    def test_promotion_keeps_recoverable_originals(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            live = root / "live"
            staged = root / "staged"
            live.write_text("old", encoding="utf-8")
            staged.write_text("new", encoding="utf-8")
            backup = root / "backup"
            atomic_promote(
                [(staged, live)],
                backup,
                lambda: self.assertEqual("new", live.read_text()),
            )
            self.assertEqual("new", live.read_text())
            self.assertEqual("old", (backup / "originals/0").read_text())
            report = json.loads((backup / "promotion.json").read_text(encoding="utf-8"))
            self.assertEqual("promoted-and-verified", report["result"])
            self.assertEqual([(backup / "originals/0", live)], rollback_replacements(backup))

    def test_failed_verification_rolls_back_every_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            replacements = []
            for name in ("support", "receipt"):
                live = root / "live" / name
                staged = root / "staged" / name
                live.parent.mkdir(exist_ok=True)
                staged.parent.mkdir(exist_ok=True)
                live.write_text(f"old-{name}", encoding="utf-8")
                staged.write_text(f"new-{name}", encoding="utf-8")
                replacements.append((staged, live))
            backup = root / "backup"
            with self.assertRaises(PromotionError):
                atomic_promote(
                    replacements,
                    backup,
                    lambda: (_ for _ in ()).throw(RuntimeError("doctor failed")),
                )
            for _staged, live in replacements:
                self.assertEqual(f"old-{live.name}", live.read_text())
            report = json.loads((backup / "promotion.json").read_text())
            self.assertEqual("failed-auto-rolled-back", report["result"])
            self.assertEqual(2, len(list((backup / "failed-new").iterdir())))


if __name__ == "__main__":
    unittest.main()
