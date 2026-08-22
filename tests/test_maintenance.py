from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from unittest import mock

from codex_openrouter import upgrade as upgrade_module
from codex_openrouter.lifecycle import LifecycleLock
from codex_openrouter.processes import matching_processes
from codex_openrouter.promotion import PromotionError, atomic_promote, rollback_replacements
from scripts import secret_scan
from scripts.build_release import FILES, copy_allowlist, tracked_paths, validate_release_version


ROOT = Path(__file__).resolve().parents[1]


class VersionAndAdapterTests(unittest.TestCase):
    def test_release_tag_is_canonical_and_matches_version_file(self) -> None:
        expected = "v" + (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        self.assertEqual(expected, validate_release_version(expected))
        for invalid in ("0.1.1", "v0.01.1", "v0.1.0", "v1.2.3-rc1"):
            with self.subTest(invalid=invalid), self.assertRaises(RuntimeError):
                validate_release_version(invalid)


class ReleasePackagingTests(unittest.TestCase):
    def test_upstream_provenance_is_a_required_release_file(self) -> None:
        self.assertIn("UPSTREAMS.md", FILES)

    def test_allowlist_ships_only_tracked_files_and_no_empty_directories(self) -> None:
        """成果物の内容をrepositoryの内容に一致させる。

        収集がfilesystem走査だった頃は、`.gitignore`済みの`.DS_Store`とgitが表現できない
        空ディレクトリがmacOSのローカルビルドでだけ配布物へ入っていた。
        """
        tracked = tracked_paths()
        with tempfile.TemporaryDirectory(prefix="release-allowlist-") as temporary:
            staged = Path(temporary)
            copy_allowlist(staged)
            produced = sorted(path.relative_to(staged) for path in staged.rglob("*"))
            self.assertTrue(produced)
            for relative in produced:
                path = staged / relative
                if path.is_dir():
                    self.assertTrue(any(path.iterdir()), f"empty directory shipped: {relative}")
                else:
                    self.assertIn(str(relative), tracked, f"untracked file shipped: {relative}")

    def test_archive_scan_rejects_os_generated_files_the_tree_scan_tolerates(self) -> None:
        for junk in (".DS_Store", "portable/.DS_Store", "._payload", "Thumbs.db", "desktop.ini"):
            with self.subTest(junk=junk):
                self.assertTrue(secret_scan.os_junk_path(junk))
        for present_in_a_macos_checkout in (".DS_Store", "portable/.DS_Store", "._payload"):
            with self.subTest(tolerated=present_in_a_macos_checkout):
                # 作業ツリーには正当に存在するので、`--tree`が使う共有述語では弾かない
                self.assertFalse(secret_scan.forbidden_path(present_in_a_macos_checkout))
        for shipped in ("README.md", "models/registry.json", "src/codex_openrouter/cli.py"):
            with self.subTest(shipped=shipped):
                self.assertFalse(secret_scan.os_junk_path(shipped))


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
    def test_app_bundle_swap_never_renames_the_live_app_across_directories(self) -> None:
        """provenance付きappのcross-directory renameはmacOSで停止しうる。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            live = root / "Desktop/Codex OpenRouter.app"
            staged = root / "staged/Codex OpenRouter.app"
            live.mkdir(parents=True)
            staged.mkdir(parents=True)
            (live / "version").write_text("old", encoding="utf-8")
            (staged / "version").write_text("new", encoding="utf-8")
            backup = root / "backup"

            with mock.patch(
                "codex_openrouter.promotion.os.replace", wraps=os.replace
            ) as replace:
                atomic_promote([(staged, live)], backup, lambda: None)

            calls = [tuple(call.args) for call in replace.call_args_list]
            adjacent = live.parent / f".{live.name}.upgrade-old"
            self.assertIn((live, adjacent), calls)
            self.assertNotIn((live, backup / "originals/0"), calls)
            self.assertEqual("new", (live / "version").read_text())
            self.assertEqual("old", (backup / "originals/0/version").read_text())
            self.assertFalse(adjacent.exists())

    def test_failed_app_bundle_verification_restores_the_adjacent_original(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            live = root / "Desktop/Codex OpenRouter.app"
            staged = root / "staged/Codex OpenRouter.app"
            live.mkdir(parents=True)
            staged.mkdir(parents=True)
            (live / "version").write_text("old", encoding="utf-8")
            (staged / "version").write_text("new", encoding="utf-8")
            backup = root / "backup"

            with self.assertRaises(PromotionError):
                atomic_promote(
                    [(staged, live)],
                    backup,
                    lambda: (_ for _ in ()).throw(RuntimeError("doctor failed")),
                )

            self.assertEqual("old", (live / "version").read_text())
            self.assertEqual("old", (backup / "originals/0/version").read_text())
            self.assertEqual("new", (backup / "failed-new/0/version").read_text())
            self.assertFalse((live.parent / f".{live.name}.upgrade-old").exists())

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

    def test_manual_rollback_removes_targets_that_were_new_in_the_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staged = root / "staged-profile.json"
            live = root / "profile.json"
            staged.write_text("new-profile", encoding="utf-8")
            upgrade_backup = root / "upgrade-backup"
            atomic_promote([(staged, live)], upgrade_backup, lambda: None)
            self.assertEqual("new-profile", live.read_text())

            rollback_backup = root / "rollback-backup"
            replacements = rollback_replacements(upgrade_backup)
            self.assertEqual([(None, live)], replacements)
            atomic_promote(replacements, rollback_backup, lambda: self.assertFalse(live.exists()))
            self.assertFalse(live.exists())
            # rollback自体も元へ戻せる。
            atomic_promote(
                rollback_replacements(rollback_backup),
                root / "undo-rollback",
                lambda: self.assertEqual("new-profile", live.read_text()),
            )


def _source_tree(root: Path) -> Path:
    """copy_support が運ぶ形の最小ツリーを作る。"""
    for name in upgrade_module.SUPPORT_TREES:
        (root / name).mkdir(parents=True)
        (root / name / f"{name}.txt").write_text(name, encoding="utf-8")
    for name in upgrade_module.SUPPORT_FILES:
        (root / name).write_text(name, encoding="utf-8")
    (root / "src/codex_openrouter").mkdir(parents=True)
    (root / "src/codex_openrouter/cli.py").write_text("print('hi')\n", encoding="utf-8")
    return root


class RuntimeDigestTests(unittest.TestCase):
    def test_installed_python_entry_points_use_the_setup_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            entry = Path(temporary) / "entry"
            entry.write_text("#!/usr/bin/env python3\nprint('ok')\n", encoding="utf-8")
            upgrade_module.pin_python_shebang(entry, "/opt/example/python3")
            self.assertEqual(
                "#!/opt/example/python3\nprint('ok')\n",
                entry.read_text(encoding="utf-8"),
            )
            self.assertEqual(0o755, entry.stat().st_mode & 0o777)

    def test_python_entry_point_with_an_unexpected_shebang_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            entry = Path(temporary) / "entry"
            entry.write_text("#!/bin/zsh\nexit 0\n", encoding="utf-8")
            with self.assertRaises(upgrade_module.UpgradeError):
                upgrade_module.pin_python_shebang(entry, "/opt/example/python3")

    def test_copy_support_reproduces_the_same_digest(self) -> None:
        """コピーする側と比較する側が同じ一覧を見ていること。

        片方だけ対象が増えると「変わったのに変わっていない」と判定されるので、
        ここが実質的な回帰検出になる。
        """
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _source_tree(root / "source")
            target = root / "installed"
            upgrade_module.copy_support(source, target)
            self.assertEqual(
                upgrade_module.runtime_digest(source), upgrade_module.runtime_digest(target)
            )

    def test_generated_directories_do_not_affect_the_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = _source_tree(Path(temporary) / "source")
            before = upgrade_module.runtime_digest(source)
            (source / "src/__pycache__").mkdir()
            (source / "src/__pycache__/cli.pyc").write_bytes(b"\x00compiled")
            (source / "portable/dist").mkdir()
            (source / "portable/dist/archive.tar.gz").write_bytes(b"artifact")
            self.assertEqual(before, upgrade_module.runtime_digest(source))

    def test_any_content_or_layout_change_moves_the_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = _source_tree(Path(temporary) / "source")
            before = upgrade_module.runtime_digest(source)
            (source / "src/codex_openrouter/cli.py").write_text("print('ho')\n", encoding="utf-8")
            after = upgrade_module.runtime_digest(source)
            self.assertNotEqual(before, after)
            # 改名も差として出る（相対pathを混ぜているため）。
            (source / "src/codex_openrouter/cli.py").rename(
                source / "src/codex_openrouter/main.py"
            )
            self.assertNotEqual(after, upgrade_module.runtime_digest(source))


class RegistryMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "source.json"
        self.installed = self.root / "installed.json"
        self.source.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "catalog_refresh": {"models_url": "https://example.invalid/models"},
                    "models": {
                        "vendor/bundled": {"display_name": "Current bundled"},
                    },
                }
            ),
            encoding="utf-8",
        )

    def test_removes_retired_provider_entries_and_keeps_openrouter_additions(self) -> None:
        self.installed.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "retired_catalog_refresh": {"models_url": "https://retired.invalid"},
                    "models": {
                        "vendor/bundled": {"display_name": "Stale bundled"},
                        "vendor/added": {
                            "display_name": "Added",
                            "router": "openrouter",
                            "upstream_id": "vendor/added",
                            "data_retention": "zdr",
                        },
                        "internal/retired": {
                            "display_name": "Retired",
                            "router": "retired-provider",
                            "upstream_id": "vendor/retired",
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

        migrated = upgrade_module.migrated_registry(self.source, self.installed)

        self.assertIsNotNone(migrated)
        assert migrated is not None
        self.assertNotIn("retired_catalog_refresh", migrated)
        self.assertEqual("Current bundled", migrated["models"]["vendor/bundled"]["display_name"])
        self.assertEqual("Added", migrated["models"]["vendor/added"]["display_name"])
        self.assertNotIn("router", migrated["models"]["vendor/added"])
        self.assertNotIn("upstream_id", migrated["models"]["vendor/added"])
        self.assertNotIn("data_retention", migrated["models"]["vendor/added"])
        self.assertTrue(migrated["models"]["vendor/added"]["zdr_supported"])
        self.assertNotIn("internal/retired", migrated["models"])

    def test_returns_none_when_the_source_registry_is_sufficient(self) -> None:
        self.installed.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "models": {
                        "vendor/bundled": {"display_name": "Stale bundled"},
                        "anything": {"router": "retired-provider"},
                    },
                }
            ),
            encoding="utf-8",
        )
        self.assertIsNone(upgrade_module.migrated_registry(self.source, self.installed))

    def test_rejects_an_openrouter_entry_that_would_change_upstream_id(self) -> None:
        self.installed.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "models": {
                        "vendor/alias": {
                            "router": "openrouter",
                            "upstream_id": "vendor/different",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(upgrade_module.UpgradeError):
            upgrade_module.migrated_registry(self.source, self.installed)


class AutoUpgradeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.home = root / "home"
        self.source = _source_tree(self.home / "repo")
        self.support = self.home / "support/current"
        self.state = self.home / "support/state"
        self.state.mkdir(parents=True)
        upgrade_module.copy_support(self.source, self.support)
        self.receipt = self.state / "install-manifest.json"
        self.paths = mock.Mock(
            home=self.home,
            support_root=self.support,
            state_dir=self.state,
            install_manifest=self.receipt,
        )
        self.write_receipt({"schema_version": 5, "source_root": str(self.source)})

    def write_receipt(self, document: dict) -> None:
        self.receipt.write_text(json.dumps(document), encoding="utf-8")

    def change_source(self) -> None:
        (self.source / "src/codex_openrouter/cli.py").write_text("print('new')\n", encoding="utf-8")

    def test_identical_trees_do_not_trigger_an_upgrade(self) -> None:
        with mock.patch.object(upgrade_module, "upgrade") as upgrade_call:
            self.assertEqual(0, upgrade_module.auto_upgrade(self.paths))
        upgrade_call.assert_not_called()

    def test_changed_source_triggers_an_offline_upgrade(self) -> None:
        self.change_source()
        with mock.patch.object(upgrade_module, "upgrade", return_value=0) as upgrade_call:
            self.assertEqual(0, upgrade_module.auto_upgrade(self.paths))
        upgrade_call.assert_called_once()
        # 実課金のAPI往復はrutimeの入れ替えに要らない。
        self.assertFalse(upgrade_call.call_args.kwargs["network_check"])
        self.assertEqual(self.source, upgrade_call.call_args.args[0])

    def test_unrecorded_source_root_is_skipped(self) -> None:
        self.write_receipt({"schema_version": 5})
        self.change_source()
        with mock.patch.object(upgrade_module, "upgrade") as upgrade_call:
            self.assertEqual(0, upgrade_module.auto_upgrade(self.paths))
        upgrade_call.assert_not_called()

    def test_missing_source_root_does_not_break_the_launch(self) -> None:
        self.write_receipt({"schema_version": 5, "source_root": str(self.home / "gone")})
        with mock.patch.object(upgrade_module, "upgrade") as upgrade_call:
            self.assertEqual(0, upgrade_module.auto_upgrade(self.paths))
        upgrade_call.assert_not_called()

    def test_source_root_outside_home_is_refused(self) -> None:
        outside = Path(self.directory.name) / "elsewhere"
        _source_tree(outside)
        self.write_receipt({"schema_version": 5, "source_root": str(outside)})
        with mock.patch.object(upgrade_module, "upgrade") as upgrade_call:
            self.assertEqual(0, upgrade_module.auto_upgrade(self.paths))
        upgrade_call.assert_not_called()

    def test_failure_never_blocks_the_launch_and_is_not_retried(self) -> None:
        self.change_source()
        with mock.patch.object(
            upgrade_module, "upgrade", side_effect=RuntimeError("promotion rolled back")
        ) as upgrade_call:
            self.assertEqual(0, upgrade_module.auto_upgrade(self.paths))
            self.assertEqual(1, upgrade_call.call_count)
            state = json.loads((self.state / "selfupdate.json").read_text(encoding="utf-8"))
            self.assertEqual("failure", state["result"])
            # 同じ内容なら再試行しない。クリックのたびに十数秒払わないため。
            self.assertEqual(0, upgrade_module.auto_upgrade(self.paths))
            self.assertEqual(1, upgrade_call.call_count)

    def test_busy_lifecycle_is_skipped_without_recording_a_failure(self) -> None:
        self.change_source()
        with LifecycleLock(self.paths):
            self.assertEqual(0, upgrade_module.auto_upgrade(self.paths))
        self.assertFalse((self.state / "selfupdate.json").exists())

    def test_a_further_source_change_retries_after_a_failure(self) -> None:
        self.change_source()
        with mock.patch.object(
            upgrade_module, "upgrade", side_effect=RuntimeError("boom")
        ) as upgrade_call:
            upgrade_module.auto_upgrade(self.paths)
            (self.source / "src/codex_openrouter/cli.py").write_text("print('fixed')\n", encoding="utf-8")
            upgrade_module.auto_upgrade(self.paths)
        self.assertEqual(2, upgrade_call.call_count)

    def test_manifest_omits_source_root_when_it_is_the_installed_tree(self) -> None:
        stock = mock.Mock(version="26.803", build="6396")
        document = upgrade_module.manifest_document(
            self.support, self.paths, stock, self.home, "abc123", "profile-digest"
        )
        self.assertNotIn("source_root", document)
        recorded = upgrade_module.manifest_document(
            self.source, self.paths, stock, self.home, "abc123", "profile-digest"
        )
        self.assertEqual(str(self.source), recorded["source_root"])
        self.assertEqual(5, recorded["schema_version"])
        self.assertEqual("profile-digest", recorded["profile_digest"])


if __name__ == "__main__":
    unittest.main()
