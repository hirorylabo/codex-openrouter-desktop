from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from codex_openrouter import configblock, supervisor as sup  # noqa: E402
from codex_openrouter.app import UserPaths  # noqa: E402
from codex_openrouter.lifecycle import LifecycleLock, LifecycleLockError  # noqa: E402
from codex_openrouter.profile import ResolvedProfile  # noqa: E402

REGISTRY_PATH = ROOT / "models/registry.json"
REGISTRY = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))["models"]

LIVE_CONFIG = """model = "gpt-5.6-sol"
model_reasoning_effort = "xhigh"

[shell_environment_policy]
inherit = "core"
"""


def make_paths(root: Path) -> UserPaths:
    return UserPaths(
        home=root,
        stock_app=root / "ChatGPT.app",
        openrouter_app=root / "clone.app",
        codex_home=root / ".codex-openrouter",
        bin_dir=root / "bin",
        support_root=root / "support",
        credential_helper=root / "bin/credential",
        desktop_launcher=root / "Desktop/Codex OpenRouter.app",
        shared_home=root / ".codex",
        state_dir=root / "state",
    )


class SupervisorTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.paths = make_paths(self.root)
        self.paths.shared_home.mkdir(parents=True)
        self.paths.shared_config.write_text(LIVE_CONFIG, encoding="utf-8")
        self.paths.composite_catalog.parent.mkdir(parents=True, exist_ok=True)
        self.paths.composite_catalog.write_text('{"models": []}', encoding="utf-8")
        self.supervisor = sup.Supervisor(self.paths, REGISTRY_PATH, port=0)

    def config_text(self) -> str:
        return self.paths.shared_config.read_text(encoding="utf-8")


class ConfigLifecycleTests(SupervisorTestCase):
    def test_apply_adds_both_blocks(self):
        self.supervisor.apply_config(8791)
        text = self.config_text()
        self.assertTrue(configblock.has_block(text, sup.CATALOG_BLOCK))
        self.assertTrue(configblock.has_block(text, sup.PROVIDER_BLOCK))
        self.assertIn("model_catalog_json", text)
        self.assertIn("http://127.0.0.1:8791/v1", text)

    def test_apply_preserves_user_config(self):
        self.supervisor.apply_config(8791)
        self.assertIn("[shell_environment_policy]", self.config_text())
        self.assertIn('model_reasoning_effort = "xhigh"', self.config_text())

    def test_cleanup_removes_catalog_but_keeps_provider(self):
        """純正起動をvanillaに戻しつつ、OR記録threadのresumeは壊さない。"""
        self.supervisor.apply_config(8791)
        self.supervisor.cleanup()
        text = self.config_text()
        self.assertFalse(configblock.has_block(text, sup.CATALOG_BLOCK))
        self.assertNotIn("model_catalog_json", text)
        self.assertTrue(configblock.has_block(text, sup.PROVIDER_BLOCK))
        self.assertIn("[model_providers.openrouter]", text)
        self.assertIn("http://127.0.0.1:0/v1", text)
        self.assertIn('command = "/usr/bin/false"', text)

    def test_apply_is_idempotent(self):
        self.supervisor.apply_config(8791)
        once = self.config_text()
        sup.Supervisor(self.paths, REGISTRY_PATH, port=0).apply_config(8791)
        self.assertEqual(self.config_text(), once)

    def test_active_provider_uses_local_token_not_keychain_helper(self):
        self.supervisor.apply_config(8791)
        text = self.config_text()
        self.assertIn(str(self.paths.guard_token), text)
        self.assertNotIn(str(self.paths.credential_helper), text)
        self.assertIn('command = "/bin/cat"', text)
        self.assertNotIn("sk-or-", text)

    def test_provider_only_cleanup_then_apply_keeps_catalog(self):
        self.supervisor.apply_config(8791)
        self.supervisor.cleanup()
        revived = sup.Supervisor(self.paths, REGISTRY_PATH, port=0)
        revived.apply_config(49152)
        text = self.config_text()
        self.assertTrue(configblock.has_block(text, sup.CATALOG_BLOCK))
        self.assertTrue(configblock.has_block(text, sup.PROVIDER_BLOCK))
        self.assertIn("http://127.0.0.1:49152/v1", text)

    def test_unmarked_provider_conflict_does_not_change_config(self):
        conflict = LIVE_CONFIG + '\n[model_providers.openrouter]\nname = "mine"\n'
        self.paths.shared_config.write_text(conflict, encoding="utf-8")
        with self.assertRaises(configblock.ConfigBlockError):
            self.supervisor.apply_config(8791)
        self.assertEqual(self.config_text(), conflict)


class SelectionRestoreTests(SupervisorTestCase):
    def test_cleanup_restores_native_model_when_openrouter_selected(self):
        self.supervisor.apply_config(8791)
        # 利用者がORモデルを選んだままappを終了した状況。
        configblock.edit(
            self.paths.shared_config,
            lambda text: configblock.upsert_top_level(
                configblock.upsert_top_level(text, "model", "z-ai/glm-5.2"),
                "model_provider",
                "openrouter",
            ),
        )
        self.supervisor.cleanup()
        text = self.config_text()
        self.assertEqual(configblock.read_top_level(text, "model"), "gpt-5.6-sol")
        self.assertNotEqual(configblock.read_top_level(text, "model_provider"), "openrouter")

    def test_cleanup_leaves_native_selection_alone(self):
        self.supervisor.apply_config(8791)
        self.supervisor.cleanup()
        self.assertEqual(configblock.read_top_level(self.config_text(), "model"), "gpt-5.6-sol")

    def test_falls_back_when_saved_model_was_also_openrouter(self):
        configblock.atomic_write(
            self.paths.shared_config,
            configblock.upsert_top_level(LIVE_CONFIG, "model", "z-ai/glm-5.2"),
        )
        self.supervisor.apply_config(8791)
        self.supervisor.cleanup()
        self.assertEqual(
            configblock.read_top_level(self.config_text(), "model"), sup.NATIVE_FALLBACK_MODEL
        )


class SelfHealTests(SupervisorTestCase):
    def test_self_heal_removes_leftover_catalog_block(self):
        """強制終了された次の起動で、残骸を掃除できること。"""
        self.supervisor.apply_config(8791)
        # cleanupを呼ばずにプロセスが消えた状況を、新しいSupervisorで再現する。
        revived = sup.Supervisor(self.paths, REGISTRY_PATH, port=0)
        actions = revived.self_heal()
        text = self.config_text()
        self.assertFalse(configblock.has_block(text, sup.CATALOG_BLOCK))
        self.assertTrue(configblock.has_block(text, sup.PROVIDER_BLOCK))
        self.assertTrue(actions)

    def test_self_heal_restores_openrouter_selection(self):
        self.supervisor.apply_config(8791)
        configblock.edit(
            self.paths.shared_config,
            lambda text: configblock.upsert_top_level(text, "model", "moonshotai/kimi-k3"),
        )
        revived = sup.Supervisor(self.paths, REGISTRY_PATH, port=0)
        revived.self_heal()
        self.assertEqual(configblock.read_top_level(self.config_text(), "model"), "gpt-5.6-sol")

    def test_self_heal_is_safe_when_nothing_to_do(self):
        self.supervisor.ensure_inactive_config()
        self.assertEqual(sup.Supervisor(self.paths, REGISTRY_PATH, port=0).self_heal(), [])

    def test_self_heal_clears_active_state_even_without_config(self):
        self.paths.shared_config.unlink()
        self.paths.guard_token.parent.mkdir(parents=True, exist_ok=True)
        self.paths.guard_token.write_text("stale", encoding="utf-8")
        sup.State(active=True, guard_port=49152, guard_nonce="old").save(
            self.paths.supervisor_state
        )

        sup.Supervisor(self.paths, REGISTRY_PATH, port=0).self_heal()

        state = sup.State.load(self.paths.supervisor_state)
        self.assertFalse(state.active)
        self.assertIsNone(state.guard_port)
        self.assertIsNone(state.guard_nonce)
        self.assertFalse(self.paths.guard_token.exists())


class UpdateFollowTests(SupervisorTestCase):
    def test_catalog_is_regenerated_when_build_changes(self):
        calls = []

        def fake_generate(codex, home, registry, output, **_kwargs):
            calls.append(output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text('{"models": []}', encoding="utf-8")
            return output

        with mock.patch.object(sup, "stock_build_id", return_value=("26.1", "6396")), \
             mock.patch.object(sup.catalog, "generate", side_effect=fake_generate):
            self.assertTrue(self.supervisor.refresh_catalog_if_needed())
            # 同じbuildなら再生成しない（毎回のコストをほぼゼロにする）。
            self.assertFalse(self.supervisor.refresh_catalog_if_needed())

        with mock.patch.object(sup, "stock_build_id", return_value=("26.2", "6400")), \
             mock.patch.object(sup.catalog, "generate", side_effect=fake_generate):
            self.assertTrue(self.supervisor.refresh_catalog_if_needed())
        self.assertEqual(len(calls), 2)

    def test_state_survives_new_instance(self):
        with mock.patch.object(sup, "stock_build_id", return_value=("26.1", "6396")), \
             mock.patch.object(sup.catalog, "generate", return_value=self.paths.composite_catalog):
            self.supervisor.refresh_catalog_if_needed()
            revived = sup.Supervisor(self.paths, REGISTRY_PATH, port=0)
            self.assertFalse(revived.refresh_catalog_if_needed())


class ExclusionTests(SupervisorTestCase):
    def test_refuses_when_stock_app_is_running(self):
        with mock.patch.object(sup, "process_pids", return_value=[1234]):
            with self.assertRaises(sup.SupervisorError):
                self.supervisor.assert_stock_not_running()

    def test_allows_when_stock_app_is_not_running(self):
        with mock.patch.object(sup, "process_pids", return_value=[]):
            self.supervisor.assert_stock_not_running()

    def test_running_stock_app_is_rejected_without_self_heal(self):
        state = b'{"active": true, "guard_port": 49152}\n'
        self.paths.supervisor_state.parent.mkdir(parents=True, exist_ok=True)
        self.paths.supervisor_state.write_bytes(state)
        self.paths.guard_token.write_text("stale-token", encoding="utf-8")
        config = self.paths.shared_config.read_bytes()

        with mock.patch.object(sup, "process_pids", return_value=[1234]), \
             mock.patch.object(self.supervisor, "self_heal") as self_heal, \
             self.assertRaises(sup.SupervisorError):
            self.supervisor.run()

        self_heal.assert_not_called()
        self.assertEqual(config, self.paths.shared_config.read_bytes())
        self.assertEqual(state, self.paths.supervisor_state.read_bytes())
        self.assertEqual("stale-token", self.paths.guard_token.read_text(encoding="utf-8"))

    def test_competing_launch_does_not_self_heal_active_config(self):
        self.supervisor.apply_config(49152)
        config = self.paths.shared_config.read_bytes()
        with LifecycleLock(self.paths), \
             mock.patch.object(self.supervisor, "self_heal") as self_heal, \
             self.assertRaises(LifecycleLockError):
            self.supervisor.run()
        self_heal.assert_not_called()
        self.assertEqual(config, self.paths.shared_config.read_bytes())


class LaunchTests(SupervisorTestCase):
    def test_launch_never_passes_real_key_to_stock_app(self):
        executable = self.paths.stock_app / "Contents/MacOS/ChatGPT"
        executable.parent.mkdir(parents=True)
        executable.touch()
        self.supervisor.workspace = self.root / "workspace"

        with mock.patch.dict(
            sup.os.environ,
            {"OPENROUTER_API_KEY": "must-not-leak", "SAFE_VALUE": "kept"},
            clear=True,
        ), mock.patch.object(sup.subprocess, "Popen", return_value=object()) as popen:
            self.supervisor.launch()

        arguments = popen.call_args.args[0]
        environment = popen.call_args.kwargs["env"]
        self.assertEqual(arguments, [str(executable), str(self.supervisor.workspace)])
        self.assertNotIn("OPENROUTER_API_KEY", environment)
        self.assertEqual(environment["SAFE_VALUE"], "kept")


class ProviderBlockTests(unittest.TestCase):
    def test_active_body_has_local_token_and_no_real_credential(self):
        body = sup.provider_block_body(8791, Path("/tmp/guard-token"))
        self.assertIn('base_url = "http://127.0.0.1:8791/v1"', body)
        self.assertIn('wire_api = "responses"', body)
        self.assertIn('command = "/bin/cat"', body)
        self.assertIn('args = ["/tmp/guard-token"]', body)
        self.assertNotIn("sk-or-", body)

    def test_inactive_body_is_non_connecting_stub(self):
        body = sup.provider_block_body(0)
        self.assertIn('base_url = "http://127.0.0.1:0/v1"', body)
        self.assertIn('command = "/usr/bin/false"', body)
        self.assertIn("args = []", body)


class ProfileRuntimeTests(SupervisorTestCase):
    def test_custom_profile_drives_guard_watcher_and_catalog(self):
        model = "minimax/minimax-m3"
        profile = ResolvedProfile(
            name="one",
            models=(model,),
            default_model=model,
            default_effort=REGISTRY[model].get("default_effort"),
            registry={model: REGISTRY[model]},
        )
        instance = sup.Supervisor(self.paths, REGISTRY_PATH, profile=profile, port=0)
        with mock.patch.object(sup.catalog, "generate", return_value=self.paths.composite_catalog) as gen:
            with mock.patch.object(sup, "stock_build_id", return_value=("26.1", "6396")):
                instance.refresh_catalog_if_needed(force=True)
        self.assertEqual(gen.call_args.kwargs["model_ids"], (model,))
        with mock.patch.object(sup.watcher_module, "Watcher") as watcher:
            watcher.return_value.start.return_value = (None, None)
            instance.start_watcher()
        watcher.assert_called_once_with(self.paths.shared_config, (model,))

    def test_profile_default_is_applied_once(self):
        self.supervisor.apply_config(8791)
        self.assertEqual(
            configblock.read_top_level(self.config_text(), "model"),
            self.supervisor.profile.default_model,
        )
        self.supervisor.cleanup()
        configblock.edit(
            self.paths.shared_config,
            lambda text: configblock.upsert_top_level(text, "model", "gpt-5.6-sol"),
        )
        revived = sup.Supervisor(self.paths, REGISTRY_PATH, port=0)
        revived.apply_config(49152)
        self.assertEqual(configblock.read_top_level(self.config_text(), "model"), "gpt-5.6-sol")


if __name__ == "__main__":
    unittest.main()
