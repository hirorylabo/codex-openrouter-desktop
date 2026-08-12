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

    def test_apply_is_idempotent(self):
        self.supervisor.apply_config(8791)
        once = self.config_text()
        sup.Supervisor(self.paths, REGISTRY_PATH, port=0).apply_config(8791)
        self.assertEqual(self.config_text(), once)

    def test_credential_helper_path_is_in_config_but_no_key(self):
        self.supervisor.apply_config(8791)
        text = self.config_text()
        self.assertIn(str(self.paths.credential_helper), text)
        self.assertNotIn("sk-or-", text)


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
        self.assertEqual(sup.Supervisor(self.paths, REGISTRY_PATH, port=0).self_heal(), [])


class UpdateFollowTests(SupervisorTestCase):
    def test_catalog_is_regenerated_when_build_changes(self):
        calls = []

        def fake_generate(codex, home, registry, output):
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


class ProviderBlockTests(unittest.TestCase):
    def test_body_has_no_secret_and_uses_loopback(self):
        body = sup.provider_block_body(8791, Path("/opt/helper"))
        self.assertIn('base_url = "http://127.0.0.1:8791/v1"', body)
        self.assertIn('wire_api = "responses"', body)
        self.assertIn('command = "/opt/helper"', body)
        self.assertNotIn("sk-or-", body)


if __name__ == "__main__":
    unittest.main()
