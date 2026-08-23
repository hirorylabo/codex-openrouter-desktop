from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from codex_openrouter import configblock, supervisor as sup, toolcompat  # noqa: E402
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

    def test_catalog_refresh_hands_the_snapshot_path_and_build(self):
        """次のapp更新でテンプレート差分を取れるよう、生成時にsnapshotを残させる。"""
        with mock.patch.object(sup, "stock_build_id", return_value=("26.2", "6720")), \
             mock.patch.object(
                 sup.catalog, "generate", return_value=self.paths.composite_catalog
             ) as generate:
            self.assertTrue(self.supervisor.refresh_catalog_if_needed())
        kwargs = generate.call_args.kwargs
        self.assertEqual(kwargs["snapshot"], self.paths.clone_template_snapshot)
        self.assertEqual(kwargs["build_id"], ("26.2", "6720"))

    def test_catalog_is_regenerated_when_the_profile_changes(self):
        """設定変更後の次回起動で、選択モデルだけのcatalogへ組み直す。"""
        model = "minimax/minimax-m3"
        narrowed = ResolvedProfile(
            name="one",
            models=(model,),
            default_model=model,
            default_effort=REGISTRY[model].get("default_effort"),
            registry={model: REGISTRY[model]},
        )
        with mock.patch.object(sup, "stock_build_id", return_value=("26.1", "6396")), \
             mock.patch.object(
                 sup.catalog, "generate", return_value=self.paths.composite_catalog
             ) as generate:
            self.assertTrue(self.supervisor.refresh_catalog_if_needed())
            self.assertFalse(self.supervisor.refresh_catalog_if_needed())
            # buildは同じでもprofileが変われば組み直す。
            changed = sup.Supervisor(
                self.paths, REGISTRY_PATH, profile=narrowed, port=0
            )
            self.assertTrue(changed.refresh_catalog_if_needed())
            self.assertFalse(changed.refresh_catalog_if_needed())
        self.assertEqual((model,), generate.call_args.kwargs["model_ids"])
        self.assertEqual(2, generate.call_count)

    def test_catalog_is_regenerated_when_effective_tool_status_changes(self):
        def fake_generate(_codex, _home, _registry, output, **_kwargs):
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text('{"models": []}', encoding="utf-8")
            return output

        with (
            mock.patch.object(sup, "stock_build_id", return_value=("26.2", "6720")),
            mock.patch.object(sup.catalog, "generate", side_effect=fake_generate) as generate,
        ):
            self.assertTrue(self.supervisor.refresh_catalog_if_needed())
            self.assertFalse(self.supervisor.refresh_catalog_if_needed())
            model = self.supervisor.profile.models[0]
            toolcompat._atomic_write(
                self.paths.tool_compatibility,
                {
                    "schema_version": 1,
                    "entries": {
                        model: {
                            "chatgpt_build": "6720",
                            "tool_contract_version": toolcompat.TOOL_CONTRACT_VERSION,
                            "status": "partial",
                            "reason": "fixture",
                            "verified_at": time.time(),
                        }
                    },
                },
            )
            self.assertTrue(self.supervisor.refresh_catalog_if_needed())
        self.assertEqual(2, generate.call_count)

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
    BUNDLE_IDENTIFIER = "com.example.stock-chat"

    def stock_executable(self):
        """純正app相当の最小構成。bundle idはInfo.plistから読ませる。"""
        executable = self.paths.stock_app / "Contents/MacOS/ChatGPT"
        executable.parent.mkdir(parents=True)
        executable.touch()
        (self.paths.stock_app / "Contents/Info.plist").write_bytes(
            sup.plistlib.dumps({"CFBundleIdentifier": self.BUNDLE_IDENTIFIER})
        )
        return executable

    def test_launch_uses_explicit_project_flag_and_never_passes_real_key(self):
        executable = self.stock_executable()
        self.supervisor.workspace = self.root / "workspace"

        with mock.patch.dict(
            sup.os.environ,
            {"OPENROUTER_API_KEY": "must-not-leak", "SAFE_VALUE": "kept"},
            clear=True,
        ), mock.patch.object(sup.subprocess, "Popen", return_value=object()) as popen, \
             mock.patch.object(self.supervisor, "deliver_workspace"):
            self.supervisor.launch()

        arguments = popen.call_args.args[0]
        environment = popen.call_args.kwargs["env"]
        self.assertEqual(
            arguments,
            [str(executable), "--open-project", str(self.supervisor.workspace)],
        )
        self.assertNotIn("OPENROUTER_API_KEY", environment)
        self.assertEqual(environment["SAFE_VALUE"], "kept")


class WorkspaceDeliveryTests(LaunchTests):
    """`--open-project` はbuild 6849で無視される。open document経路でも必ず渡す。"""

    def run_delivery(self, returncodes, pids=(4321,)):
        self.stock_executable()
        completed = [mock.Mock(returncode=code) for code in returncodes]
        with mock.patch.object(sup, "process_pids", return_value=list(pids)), \
             mock.patch.object(sup.time, "sleep"), \
             mock.patch.object(sup.subprocess, "run", side_effect=completed) as run:
            self.supervisor.deliver_workspace()
        return run

    def test_workspace_is_delivered_through_the_open_document_path(self):
        workspace = self.root / "workspace"
        self.supervisor.workspace = workspace
        run = self.run_delivery([0, 0])
        for call in run.call_args_list:
            self.assertEqual(
                ["/usr/bin/open", "-b", self.BUNDLE_IDENTIFIER, str(workspace)],
                call.args[0],
            )
        # appの復元に上書きされないよう、落ち着かせてから複数回送る。
        self.assertEqual(sup.WORKSPACE_DELIVERY_REPEATS, run.call_count)

    def test_delivery_waits_before_each_send(self):
        """早すぎるopenはappの前回project復元に上書きされる。必ず待ってから送る。"""
        self.supervisor.workspace = self.root / "workspace"
        self.stock_executable()
        with mock.patch.object(sup, "process_pids", return_value=[4321]), \
             mock.patch.object(sup.time, "sleep") as sleep, \
             mock.patch.object(sup.subprocess, "run", return_value=mock.Mock(returncode=0)):
            self.supervisor.deliver_workspace()
        settles = [call.args[0] for call in sleep.call_args_list]
        self.assertEqual(
            [sup.WORKSPACE_SETTLE_SECONDS] * sup.WORKSPACE_DELIVERY_REPEATS, settles
        )

    def test_no_workspace_sends_nothing(self):
        self.supervisor.workspace = None
        self.stock_executable()
        with mock.patch.object(sup.subprocess, "run") as run:
            self.supervisor.deliver_workspace()
        run.assert_not_called()

    def test_delivery_retries_and_then_fails_closed(self):
        self.supervisor.workspace = self.root / "workspace"
        self.stock_executable()
        with mock.patch.object(sup, "process_pids", return_value=[4321]), \
             mock.patch.object(sup.time, "sleep"), \
             mock.patch.object(sup.time, "monotonic", side_effect=[0.0, 0.0, 1.0, 999.0]), \
             mock.patch.object(sup.subprocess, "run", return_value=mock.Mock(returncode=1)), \
             self.assertRaises(sup.SupervisorError):
            self.supervisor.deliver_workspace()

    def test_launch_stops_the_app_when_the_workspace_cannot_be_delivered(self):
        self.stock_executable()
        self.supervisor.workspace = self.root / "workspace"
        process = mock.Mock()
        with mock.patch.object(sup.subprocess, "Popen", return_value=process), \
             mock.patch.object(
                 self.supervisor, "deliver_workspace", side_effect=sup.SupervisorError("boom")
             ), \
             self.assertRaises(sup.SupervisorError):
            self.supervisor.launch()
        process.terminate.assert_called_once_with()

    def test_bundle_identifier_comes_from_the_stock_app_plist(self):
        self.stock_executable()
        self.assertEqual(
            self.BUNDLE_IDENTIFIER, sup.stock_bundle_identifier(self.paths.stock_app)
        )

    def test_missing_bundle_identifier_is_rejected(self):
        self.stock_executable()
        (self.paths.stock_app / "Contents/Info.plist").write_bytes(sup.plistlib.dumps({}))
        with self.assertRaises(sup.SupervisorError):
            sup.stock_bundle_identifier(self.paths.stock_app)


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
    def test_unknown_build_blocks_guard_before_keychain_access(self):
        with (
            mock.patch.object(sup, "stock_build_id", return_value=("26.9", "7000")),
            mock.patch.object(sup, "CredentialStore") as credential_store,
            self.assertRaisesRegex(sup.SupervisorError, "7000"),
        ):
            self.supervisor.start_guard()
        credential_store.assert_not_called()

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
        with mock.patch.object(sup.guard_module, "serve") as serve, \
                mock.patch.object(sup.guard_module, "health_ok", return_value=True), \
                mock.patch.object(sup, "stock_build_id", return_value=("26.1", "6396")), \
                mock.patch.object(sup.toolbridge, "assert_supported_build"):
            serve.return_value = (mock.Mock(), 0)
            instance.start_guard()
        self.assertEqual(instance.guard.review_model, model)
        with mock.patch.object(sup.catalog, "generate", return_value=self.paths.composite_catalog) as gen:
            with mock.patch.object(sup, "stock_build_id", return_value=("26.1", "6396")):
                instance.refresh_catalog_if_needed(force=True)
        self.assertEqual(gen.call_args.kwargs["model_ids"], (model,))
        with mock.patch.object(sup.watcher_module, "Watcher") as watcher:
            watcher.return_value.start.return_value = (None, None)
            instance.start_watcher()
        watcher.assert_called_once_with(self.paths.shared_config, (model,))

    def test_profile_default_is_applied_once(self):
        """複数model profileでは既定modelを一度だけ適用する（既存契約）。"""
        profile = self.multi_model_profile()
        first = sup.Supervisor(self.paths, REGISTRY_PATH, profile=profile, port=0)
        first.apply_config(8791)
        self.assertEqual(
            configblock.read_top_level(self.config_text(), "model"),
            profile.default_model,
        )
        first.cleanup()
        self.assertEqual(configblock.read_top_level(self.config_text(), "model"), "gpt-5.6-sol")

        revived = sup.Supervisor(self.paths, REGISTRY_PATH, profile=profile, port=0)
        self.assertFalse(revived.state.pending_default_model)
        revived.apply_config(49152)
        self.assertEqual(configblock.read_top_level(self.config_text(), "model"), "gpt-5.6-sol")

    @staticmethod
    def multi_model_profile() -> ResolvedProfile:
        models = ("deepseek/deepseek-v4-flash-0731", "z-ai/glm-5.2")
        return ResolvedProfile(
            name="multi",
            models=models,
            default_model=models[0],
            default_effort=REGISTRY[models[0]].get("default_effort"),
            registry={model: REGISTRY[model] for model in models},
        )


class SingleModelSelectionTests(SupervisorTestCase):
    """単一model profileでは、専用起動のたびにそのmodelを選び直す。"""

    def prime_pending(self) -> sup.Supervisor:
        """pending_default_modelを消化し、nativeへ戻した状態から始める。"""
        self.supervisor.apply_config(8791)
        self.supervisor.cleanup()
        self.assertEqual(configblock.read_top_level(self.config_text(), "model"), "gpt-5.6-sol")
        revived = sup.Supervisor(self.paths, REGISTRY_PATH, port=0)
        self.assertEqual(1, len(revived.profile.models))
        self.assertFalse(revived.state.pending_default_model)
        return revived

    def test_single_model_is_selected_even_when_pending_is_false(self):
        revived = self.prime_pending()
        revived.apply_config(49152)
        text = self.config_text()
        self.assertEqual(
            configblock.read_top_level(text, "model"), revived.profile.default_model
        )
        self.assertEqual(configblock.read_top_level(text, "model_provider"), "openrouter")

    def test_single_model_cleanup_restores_native_model_and_provider(self):
        revived = self.prime_pending()
        revived.apply_config(49152)
        revived.cleanup()
        text = self.config_text()
        self.assertEqual(configblock.read_top_level(text, "model"), "gpt-5.6-sol")
        self.assertEqual(configblock.read_top_level(text, "model_provider"), "openai")

    def test_multi_model_profile_keeps_native_selection_when_pending_is_false(self):
        profile = ProfileRuntimeTests.multi_model_profile()
        first = sup.Supervisor(self.paths, REGISTRY_PATH, profile=profile, port=0)
        first.apply_config(8791)
        first.cleanup()
        revived = sup.Supervisor(self.paths, REGISTRY_PATH, profile=profile, port=0)
        self.assertFalse(revived.state.pending_default_model)
        revived.apply_config(49152)
        text = self.config_text()
        self.assertEqual(configblock.read_top_level(text, "model"), "gpt-5.6-sol")
        self.assertNotEqual(configblock.read_top_level(text, "model_provider"), "openrouter")


if __name__ == "__main__":
    unittest.main()
