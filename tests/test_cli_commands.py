"""`rollback` と `migrate` を mock 無しで最後まで走らせる。

`tests/test_lifecycle_lock.py` は wrapper（`LifecycleLock` を取る側）だけを見ており、
`_rollback_locked` / `_migrate_locked` を丸ごと差し替える。lockの検証としては正しいが、
その結果 helper 本体は一度も実行されず、helper が **別関数のローカルimportで束縛された名前**を
参照していた期間の `NameError` を誰も踏まなかった（`task/0815-cli-import-hardening.md`）。

ここでは helper を直接呼び、確認入力とprocess検査だけを差し替えて実処理を通す。
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from codex_openrouter import cli  # noqa: E402
from codex_openrouter.promotion import atomic_promote  # noqa: E402
from tests_support import make_paths  # noqa: E402

LIVE_CONFIG = """model = "gpt-5.6-sol"
model_reasoning_effort = "xhigh"
"""


class CliCommandTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.paths = make_paths(self.root)
        self.paths.shared_home.mkdir(parents=True)
        self.paths.shared_config.write_text(LIVE_CONFIG, encoding="utf-8")
        # `cli.root()` が読む同梱registry/profileはリポジトリの実物を使う。
        self.enterContext(
            mock.patch.dict("os.environ", {"CODEX_OPENROUTER_SOURCE_ROOT": str(ROOT)})
        )
        # `/bin/ps` を叩かせない。ここで見たいのは「稼働中でない」場合の本流。
        self.enterContext(mock.patch.object(cli, "process_pids", return_value=[]))

    def answer(self, value: str):
        return mock.patch("builtins.input", return_value=value)


class RollbackCommandTests(CliCommandTestCase):
    def install_support_tree(self) -> None:
        """rollback後の `verify()` が読むinstall済みツリーを最小構成で置く。"""
        for relative in ("models/registry.json", "profiles/default.json"):
            target = self.paths.support_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, target)
        doctor = self.paths.bin_dir / "codex-openrouter-doctor"
        doctor.parent.mkdir(parents=True, exist_ok=True)
        doctor.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        doctor.chmod(0o755)

    def promote_once(self) -> Path:
        """upgrade済みの状態を作る。戻り値はrollback元になるbackup。"""
        live = self.paths.support_root / "VERSION"
        live.parent.mkdir(parents=True, exist_ok=True)
        live.write_text("0.1.1\n", encoding="utf-8")
        staged = self.root / "staged-VERSION"
        staged.write_text("0.2.0\n", encoding="utf-8")
        backup = self.paths.state_dir / "upgrade-backups" / "20260101-000000"
        atomic_promote([(staged, live)], backup, lambda: None)
        self.assertEqual("0.2.0\n", live.read_text(encoding="utf-8"))
        return backup

    def test_rollback_restores_the_pre_upgrade_tree(self) -> None:
        self.install_support_tree()
        backup = self.promote_once()
        with self.answer("ROLLBACK"):
            self.assertEqual(0, cli._rollback_locked(self.paths))
        self.assertEqual(
            "0.1.1\n", (self.paths.support_root / "VERSION").read_text(encoding="utf-8")
        )
        rollback_backups = [
            path
            for path in (self.paths.state_dir / "upgrade-backups").iterdir()
            if path.name.startswith("manual-rollback-")
        ]
        self.assertEqual(1, len(rollback_backups))
        report = json.loads((rollback_backups[0] / "promotion.json").read_text(encoding="utf-8"))
        self.assertEqual("promoted-and-verified", report["result"])
        self.assertNotEqual(backup, rollback_backups[0])

    def test_rollback_stops_when_the_confirmation_does_not_match(self) -> None:
        self.install_support_tree()
        self.promote_once()
        with self.answer("yes"), self.assertRaises(cli.CliError):
            cli._rollback_locked(self.paths)
        self.assertEqual(
            "0.2.0\n", (self.paths.support_root / "VERSION").read_text(encoding="utf-8")
        )

    def test_rollback_reports_when_there_is_nothing_to_restore(self) -> None:
        self.install_support_tree()
        with self.assertRaises(cli.CliError):
            cli._rollback_locked(self.paths)


class MigrateCommandTests(CliCommandTestCase):
    def test_migrate_persists_the_provider_block_and_compacts_the_legacy_home(self) -> None:
        disposable = self.paths.codex_home / "candidates/clone.app"
        disposable.mkdir(parents=True)
        (disposable / "payload").write_bytes(b"x" * 2048)
        keep = self.paths.codex_home / "sessions"
        keep.mkdir(parents=True)
        (keep / "thread.jsonl").write_text("{}\n", encoding="utf-8")

        self.assertEqual(0, cli._migrate_locked(SimpleNamespace(keep_all=False), self.paths))

        text = self.paths.shared_config.read_text(encoding="utf-8")
        self.assertIn("model_providers.openrouter", text)
        self.assertFalse((self.paths.codex_home / "candidates").exists())
        self.assertTrue((keep / "thread.jsonl").is_file())

    def test_migrate_keeps_the_legacy_home_intact_with_keep_all(self) -> None:
        disposable = self.paths.codex_home / "candidates"
        disposable.mkdir(parents=True)
        (disposable / "payload").write_bytes(b"x" * 2048)

        self.assertEqual(0, cli._migrate_locked(SimpleNamespace(keep_all=True), self.paths))

        self.assertTrue((disposable / "payload").is_file())

    def test_migrate_requires_the_shared_config(self) -> None:
        self.paths.shared_config.unlink()
        with self.assertRaises(cli.CliError):
            cli._migrate_locked(SimpleNamespace(keep_all=False), self.paths)


if __name__ == "__main__":
    unittest.main()
