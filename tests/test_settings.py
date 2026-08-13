from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from codex_openrouter import cli, settings
from codex_openrouter.openrouter import OpenRouterError
from codex_openrouter.profile import ProfileError, resolve_profile
from codex_openrouter.supervisor import State
from tests_support import make_paths


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "models/registry.json"
ALL_MODELS = tuple(json.loads(REGISTRY.read_text(encoding="utf-8"))["models"])
KEY = "sk-or-v1-" + "e" * 64


class SettingsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.paths = make_paths(self.root)
        self.paths.state_dir.mkdir(parents=True)
        (self.root / "Documents").mkdir()

        self.profile = resolve_profile(REGISTRY, ROOT / "profiles/default.json")
        self.paths.installed_profile.write_text(
            json.dumps(self.profile.as_json(), indent=2) + "\n", encoding="utf-8"
        )
        State(profile_digest=self.profile.digest).save(self.paths.supervisor_state)
        self.paths.install_manifest.write_text(
            json.dumps(
                {
                    "schema_version": 5,
                    "workspace": str(self.root / "Documents"),
                    "profile_digest": self.profile.digest,
                    # クリック起動時の自動更新はこの2つだけが手がかり。
                    "source_root": str(self.root / "repo"),
                    "source_digest": "0" * 64,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        self.write_catalogs()

    # --- helpers -----------------------------------------------------------
    def write_catalogs(self) -> None:
        from codex_openrouter import catalog

        self.paths.composite_catalog.parent.mkdir(parents=True, exist_ok=True)
        self.paths.composite_catalog.write_text('{"models": []}', encoding="utf-8")
        catalog.previous_path(self.paths.composite_catalog).write_text(
            '{"models": ["old"]}', encoding="utf-8"
        )

    def tracked(self) -> list[Path]:
        from codex_openrouter import catalog

        return [
            self.paths.installed_profile,
            self.paths.supervisor_state,
            self.paths.install_manifest,
            self.paths.composite_catalog,
            catalog.previous_path(self.paths.composite_catalog),
        ]

    def snapshot(self) -> dict[str, bytes | None]:
        return {
            str(path): path.read_bytes() if path.is_file() else None
            for path in self.tracked()
        }

    def apply(self, payload: object, *, key: str | None = KEY, validate=None):
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        store = mock.Mock()
        if key is None:
            store.get.side_effect = RuntimeError("Keychainを開けません")
        else:
            store.get.return_value = key
        with (
            mock.patch.object(settings, "CredentialStore", return_value=store),
            mock.patch.object(
                settings,
                "validate_key_and_profile",
                side_effect=validate,
                return_value={"limit": 10},
            ) as validated,
        ):
            result = settings.apply_payload(self.paths, REGISTRY, raw)
        self.validated = validated
        return result

    def selection(self, models: list[str], default: str) -> dict:
        return {"schema_version": 1, "models": models, "default_model": default}


class ShowTests(SettingsTestCase):
    def test_show_exposes_the_full_registry_and_the_installed_selection(self) -> None:
        document = settings.show_document(self.paths, REGISTRY)
        self.assertEqual(1, document["schema_version"])
        self.assertEqual(list(ALL_MODELS), [item["id"] for item in document["available"]])
        self.assertEqual(list(self.profile.models), document["profile"]["models"])
        self.assertEqual(self.profile.default_model, document["profile"]["default_model"])
        self.assertEqual(self.profile.digest, document["profile"]["digest"])
        self.assertEqual(str(self.root / "Documents"), document["workspace"])
        self.assertTrue(document["editable"])
        self.assertFalse(document["openrouter_active"])
        for item in document["available"]:
            self.assertTrue(item["display_name"])
            self.assertIsInstance(item["efforts"], list)

    def test_show_never_carries_a_secret(self) -> None:
        rendered = json.dumps(settings.show_document(self.paths, REGISTRY))
        self.assertNotIn("sk-or-", rendered)
        self.assertNotIn(str(self.paths.credential_helper), rendered)

    def test_running_openrouter_mode_disables_editing(self) -> None:
        State(profile_digest=self.profile.digest, active=True, guard_port=49152).save(
            self.paths.supervisor_state
        )
        with mock.patch.object(settings, "process_pids", return_value=[1234]):
            document = settings.show_document(self.paths, REGISTRY)
        self.assertTrue(document["openrouter_active"])
        self.assertFalse(document["editable"])

    def test_stale_active_state_after_a_crash_still_allows_editing(self) -> None:
        """SIGKILL後のactive残骸で設定画面が永久に編集不可にならないこと。

        self-healは次の専用起動まで走らない。stateだけを信じると、その間ずっと
        「ChatGPT終了後に変更できます」と出したまま何も変えられなくなる。
        """
        State(profile_digest=self.profile.digest, active=True, guard_port=49152).save(
            self.paths.supervisor_state
        )
        with mock.patch.object(settings, "process_pids", return_value=[]):
            document = settings.show_document(self.paths, REGISTRY)
        self.assertFalse(document["openrouter_active"])
        self.assertTrue(document["editable"])


class RejectionTests(SettingsTestCase):
    def test_invalid_selections_change_nothing(self) -> None:
        one = ALL_MODELS[0]
        cases = {
            "broken JSON": "{not json",
            "not an object": "[]",
            "empty selection": self.selection([], one),
            "unknown slug": self.selection(["unverified/model"], "unverified/model"),
            "duplicate": self.selection([one, one], one),
            "default outside selection": self.selection([one], ALL_MODELS[1]),
            "wrong schema version": {
                "schema_version": 2,
                "models": [one],
                "default_model": one,
            },
            "unsupported field": {
                "schema_version": 1,
                "models": [one],
                "default_model": one,
                "display_name": "偽名",
            },
            "reordering attempt": {
                "schema_version": 1,
                "models": list(reversed(ALL_MODELS)),
                "default_model": one,
                "order": "custom",
            },
        }
        for label, payload in cases.items():
            with self.subTest(case=label):
                before = self.snapshot()
                with self.assertRaises((ProfileError, settings.SettingsError)):
                    self.apply(payload)
                self.assertEqual(before, self.snapshot())

    def test_keychain_failure_changes_nothing(self) -> None:
        before = self.snapshot()
        with self.assertRaises(RuntimeError):
            self.apply(self.selection([ALL_MODELS[0]], ALL_MODELS[0]), key=None)
        self.assertEqual(before, self.snapshot())

    def test_guardrail_mismatch_changes_nothing(self) -> None:
        before = self.snapshot()
        with self.assertRaises(OpenRouterError):
            self.apply(
                self.selection([ALL_MODELS[0]], ALL_MODELS[0]),
                validate=OpenRouterError("missing=..., extra=..."),
            )
        self.assertEqual(before, self.snapshot())

    def test_network_failure_changes_nothing(self) -> None:
        before = self.snapshot()
        with self.assertRaises(OpenRouterError):
            self.apply(
                self.selection([ALL_MODELS[0]], ALL_MODELS[0]),
                validate=OpenRouterError("OpenRouter APIへ接続できません: URLError"),
            )
        self.assertEqual(before, self.snapshot())

    def test_apply_without_an_installation_is_refused(self) -> None:
        self.paths.install_manifest.unlink()
        before = self.snapshot()
        with self.assertRaises(settings.SettingsError):
            self.apply(self.selection([ALL_MODELS[0]], ALL_MODELS[0]))
        self.assertEqual(before, self.snapshot())

    def test_promotion_verification_failure_restores_every_target(self) -> None:
        from codex_openrouter.promotion import PromotionError

        before = self.snapshot()
        with (
            mock.patch.object(
                settings,
                "verify_promotion",
                side_effect=settings.SettingsError("synthetic verification failure"),
            ),
            self.assertRaises(PromotionError),
        ):
            self.apply(self.selection([ALL_MODELS[0]], ALL_MODELS[0]))
        self.assertEqual(before, self.snapshot())


class ApplyTests(SettingsTestCase):
    def test_single_model_selection_is_promoted_and_old_catalogs_are_dropped(self) -> None:
        from codex_openrouter import catalog

        model = ALL_MODELS[1]
        result = self.apply(self.selection([model], model))
        self.validated.assert_called_once_with(KEY, {model})

        self.assertEqual("applied", result["result"])
        self.assertTrue(result["pending_default_model"])
        promoted = resolve_profile(REGISTRY, self.paths.installed_profile)
        self.assertEqual((model,), promoted.models)
        self.assertEqual(model, promoted.default_model)
        # 表示名やeffortはregistryのまま。applyの入力経路を持たない。
        self.assertEqual(self.profile.name, promoted.name)

        state = State.load(self.paths.supervisor_state)
        self.assertEqual(promoted.digest, state.profile_digest)
        self.assertTrue(state.pending_default_model)
        manifest = json.loads(self.paths.install_manifest.read_text(encoding="utf-8"))
        self.assertEqual(promoted.digest, manifest["profile_digest"])
        self.assertEqual(str(self.root / "Documents"), manifest["workspace"])
        # 自動更新の手がかりを落とすと、以後クリックしても更新されなくなる。
        self.assertEqual(str(self.root / "repo"), manifest["source_root"])
        self.assertEqual("0" * 64, manifest["source_digest"])

        self.assertFalse(self.paths.composite_catalog.exists())
        self.assertFalse(catalog.previous_path(self.paths.composite_catalog).exists())

    def test_selection_order_follows_the_registry_not_the_request(self) -> None:
        requested = list(reversed(ALL_MODELS))
        self.apply(self.selection(requested, requested[0]))
        promoted = resolve_profile(REGISTRY, self.paths.installed_profile)
        self.assertEqual(list(ALL_MODELS), list(promoted.models))

    def test_resaving_the_same_selection_is_a_no_op(self) -> None:
        state = State.load(self.paths.supervisor_state)
        self.assertFalse(state.pending_default_model)
        before = self.snapshot()

        result = self.apply(self.selection(list(self.profile.models), self.profile.default_model))

        self.assertEqual("unchanged", result["result"])
        self.assertFalse(result["pending_default_model"])
        # Keychainにも実効モデル集合の検証にも触れない。
        self.validated.assert_not_called()
        self.assertEqual(before, self.snapshot())

    def test_default_model_is_armed_once_per_real_change(self) -> None:
        model = ALL_MODELS[2]
        self.apply(self.selection([model], model))
        self.assertTrue(State.load(self.paths.supervisor_state).pending_default_model)

        # 起動が既定モデルを適用した後の状態。
        applied = State.load(self.paths.supervisor_state)
        applied.pending_default_model = False
        applied.save(self.paths.supervisor_state)

        self.write_catalogs()
        result = self.apply(self.selection([model], model))
        self.assertEqual("unchanged", result["result"])
        self.assertFalse(State.load(self.paths.supervisor_state).pending_default_model)
        self.assertTrue(self.paths.composite_catalog.is_file())

    def test_apply_keeps_the_live_session_fields_of_the_supervisor_state(self) -> None:
        State(
            profile_digest=self.profile.digest,
            version="26.803",
            build="6396",
            saved_model="gpt-5.6-sol",
            saved_provider="openai",
        ).save(self.paths.supervisor_state)
        model = ALL_MODELS[3]
        self.apply(self.selection([model], model))
        state = State.load(self.paths.supervisor_state)
        self.assertEqual(("26.803", "6396"), (state.version, state.build))
        self.assertEqual("gpt-5.6-sol", state.saved_model)
        self.assertEqual("openai", state.saved_provider)


class SingleModelConsistencyTests(SettingsTestCase):
    """1モデル構成で picker・guard・watcher・doctor が同じ1件だけを扱うこと。

    profileはこの4者の共通の出所なので、applyが通った瞬間から全員が同じ集合を
    見ていなければ「pickerには出るがguardが止める」ような組み合わせが生まれる。
    """

    def test_every_component_sees_exactly_the_selected_model(self) -> None:
        from codex_openrouter import catalog, guard as guard_module, watcher as watcher_module

        model = ALL_MODELS[4]
        self.apply(self.selection([model], model))
        profile = resolve_profile(REGISTRY, self.paths.installed_profile)
        self.assertEqual((model,), profile.models)

        native = {
            "slug": "gpt-5.6-sol",
            "display_name": "GPT",
            "description": "native",
            "visibility": "list",
            "priority": 1,
            "supported_reasoning_levels": [{"effort": "low", "description": "Fast"}],
            "default_reasoning_level": "low",
        }
        document = catalog.build([native], profile.registry)
        catalog.validate(document, profile.registry, ALL_MODELS)
        listed = [
            entry["slug"]
            for entry in document["models"]
            if entry["display_name"].startswith(catalog.OR_PREFIX)
        ]
        self.assertEqual([model], listed)

        guard = guard_module.Guard(allowed_models=profile.models, key_provider=lambda: KEY)
        self.assertTrue(guard.allows(model))
        watcher = watcher_module.Watcher(self.paths.shared_config, profile.models)
        self.assertEqual("openrouter", watcher.desired_provider(model))
        for other in ALL_MODELS:
            if other == model:
                continue
            self.assertFalse(guard.allows(other))
            self.assertEqual("openai", watcher.desired_provider(other))

        document = settings.show_document(self.paths, REGISTRY)
        self.assertEqual([model], document["profile"]["models"])
        self.assertEqual(len(ALL_MODELS), len(document["available"]))


class CommandTests(SettingsTestCase):
    """ランチャーが実際に叩く入口。argparseとJSON出力までを通しで見る。"""

    def run_cli(self, argv: list[str], stdin: str = "") -> tuple[int, str, str]:
        store = mock.Mock()
        store.get.return_value = KEY
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(cli.UserPaths, "current", return_value=self.paths),
            mock.patch.object(cli, "root", return_value=ROOT),
            mock.patch.object(settings, "CredentialStore", return_value=store),
            mock.patch.object(settings, "validate_key_and_profile", return_value={"limit": 10}),
            mock.patch("sys.stdin", io.StringIO(stdin)),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_show_prints_a_document_the_launcher_can_parse(self) -> None:
        code, out, _err = self.run_cli(["profile", "show", "--json"])
        self.assertEqual(0, code)
        document = json.loads(out)
        self.assertEqual(1, document["schema_version"])
        self.assertEqual(list(ALL_MODELS), [item["id"] for item in document["available"]])

    def test_output_format_flags_are_mandatory(self) -> None:
        for argv in (["profile", "show"], ["profile", "apply"], ["profile"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                self.run_cli(argv)

    def test_apply_reads_stdin_and_reports_the_outcome(self) -> None:
        model = ALL_MODELS[0]
        code, out, _err = self.run_cli(
            ["profile", "apply", "--stdin-json"],
            stdin=json.dumps(self.selection([model], model)),
        )
        self.assertEqual(0, code)
        document = json.loads(out)
        self.assertEqual("applied", document["result"])
        self.assertEqual([model], document["profile"]["models"])
        self.assertEqual(
            [model], list(resolve_profile(REGISTRY, self.paths.installed_profile).models)
        )

    def test_rejected_input_exits_non_zero_with_a_readable_message(self) -> None:
        before = self.snapshot()
        code, out, err = self.run_cli(["profile", "apply", "--stdin-json"], stdin="{")
        self.assertEqual(1, code)
        self.assertEqual("", out)
        self.assertIn("ERROR:", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(before, self.snapshot())


if __name__ == "__main__":
    unittest.main()
