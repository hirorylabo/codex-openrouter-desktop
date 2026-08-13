from __future__ import annotations

import multiprocessing
from pathlib import Path
import stat
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from codex_openrouter.lifecycle import LifecycleLock, LifecycleLockError  # noqa: E402
from codex_openrouter import cli, install as install_module, upgrade as upgrade_module  # noqa: E402


def _hold_lock(state_dir: str, ready) -> None:
    paths = SimpleNamespace(state_dir=Path(state_dir))
    with LifecycleLock(paths):
        ready.send(True)
        time.sleep(60)


class LifecycleLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.paths = SimpleNamespace(state_dir=Path(self.directory.name) / "state")

    def test_second_holder_is_rejected_before_entering_body(self) -> None:
        entered = False
        with LifecycleLock(self.paths):
            with self.assertRaises(LifecycleLockError):
                with LifecycleLock(self.paths):
                    entered = True
        self.assertFalse(entered)

    def test_existing_unlocked_file_is_reused_and_normalized_to_0600(self) -> None:
        lock_path = self.paths.state_dir / "lifecycle.lock"
        lock_path.parent.mkdir(parents=True)
        lock_path.write_text("stale", encoding="utf-8")
        lock_path.chmod(0o644)

        with LifecycleLock(self.paths):
            self.assertEqual(0o600, stat.S_IMODE(lock_path.stat().st_mode))
        self.assertTrue(lock_path.is_file())

    def test_symlink_lock_is_rejected(self) -> None:
        self.paths.state_dir.mkdir(parents=True)
        target = self.paths.state_dir / "target"
        target.write_text("do not follow", encoding="utf-8")
        (self.paths.state_dir / "lifecycle.lock").symlink_to(target)

        with self.assertRaises(LifecycleLockError):
            with LifecycleLock(self.paths):
                pass
        self.assertEqual("do not follow", target.read_text(encoding="utf-8"))

    def test_kernel_releases_lock_after_holder_is_terminated(self) -> None:
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=False)
        process = context.Process(
            target=_hold_lock,
            args=(str(self.paths.state_dir), child),
        )
        process.start()
        self.addCleanup(lambda: process.is_alive() and process.kill())
        self.assertTrue(parent.poll(10), "child did not acquire lifecycle lock")
        self.assertTrue(parent.recv())

        process.terminate()
        process.join(10)
        self.assertFalse(process.is_alive())
        with LifecycleLock(self.paths):
            self.assertTrue((self.paths.state_dir / "lifecycle.lock").is_file())

    def test_install_and_upgrade_stop_before_their_operation_when_busy(self) -> None:
        with LifecycleLock(self.paths), \
             mock.patch.object(install_module, "_install_unlocked") as install, \
             mock.patch.object(upgrade_module, "_upgrade_unlocked") as upgrade:
            with self.assertRaises(LifecycleLockError):
                install_module.install(Path("source"), self.paths)
            with self.assertRaises(LifecycleLockError):
                upgrade_module.upgrade(Path("source"), self.paths)
        install.assert_not_called()
        upgrade.assert_not_called()

    def test_rollback_and_migrate_stop_before_their_operation_when_busy(self) -> None:
        args = SimpleNamespace(keep_all=False)
        with LifecycleLock(self.paths), \
             mock.patch.object(cli.UserPaths, "current", return_value=self.paths), \
             mock.patch.object(cli, "_rollback_locked") as rollback, \
             mock.patch.object(cli, "_migrate_locked") as migrate:
            with self.assertRaises(LifecycleLockError):
                cli.rollback_command(args)
            with self.assertRaises(LifecycleLockError):
                cli.migrate_command(args)
        rollback.assert_not_called()
        migrate.assert_not_called()

    def test_setup_stops_before_authentication_when_busy(self) -> None:
        args = SimpleNamespace(profile=None, auth="paste", workspace="/tmp/workspace")
        with LifecycleLock(self.paths), \
             mock.patch.object(cli.UserPaths, "current", return_value=self.paths), \
             mock.patch.object(cli, "obtain_key") as obtain_key:
            with self.assertRaises(LifecycleLockError):
                cli.setup_command(args)
        obtain_key.assert_not_called()


if __name__ == "__main__":
    unittest.main()
