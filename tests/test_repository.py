from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock
import urllib.error


ROOT = Path(__file__).resolve().parents[1]


from codex_openrouter import doctor as doctor_module
from codex_openrouter import upgrade as upgrade_module


class RepositoryTests(unittest.TestCase):
    def test_current_docs_have_no_retired_v01_commands_or_assets(self) -> None:
        texts = "\n".join(
            (ROOT / name).read_text(encoding="utf-8")
            for name in ("README.md", "README.en.md", "SECURITY.md", "CONTRIBUTING.md")
        )
        for retired in (
            "portable/patcher-js",
            "adapters/index.json",
            "codex-openrouter update",
            "creates a dedicated local clone",
        ):
            self.assertNotIn(retired, texts)

    def test_no_packaged_secrets_apps_asar_or_runtime_databases(self) -> None:
        forbidden_suffixes = {".asar", ".db", ".sqlite", ".sqlite3"}
        forbidden_names = {"auth.json", ".env", "Cookies", "Login Data"}
        problems = []
        for path in ROOT.rglob("*"):
            if ".git" in path.parts or "node_modules" in path.parts or "__pycache__" in path.parts:
                continue
            if path.is_dir() and path.suffix == ".app":
                problems.append(str(path.relative_to(ROOT)))
            if path.is_file() and (path.suffix in forbidden_suffixes or path.name in forbidden_names):
                problems.append(str(path.relative_to(ROOT)))
        self.assertEqual([], problems)

    def test_no_openrouter_key_literal(self) -> None:
        key_pattern = re.compile(rb"sk-or-v1-[A-Za-z0-9_-]{20,}")
        matches = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or any(part in {".git", "node_modules", "__pycache__"} for part in path.parts):
                continue
            if key_pattern.search(path.read_bytes()):
                matches.append(str(path.relative_to(ROOT)))
        self.assertEqual([], matches)

    def test_registry_and_profile_inventories_are_consistent(self) -> None:
        registry = json.loads((ROOT / "models/registry.json").read_text(encoding="utf-8"))
        profile = json.loads((ROOT / "profiles/default.json").read_text(encoding="utf-8"))
        self.assertEqual(1, registry["schema_version"])
        self.assertEqual(set(profile["models"]), set(registry["models"]))
        self.assertTrue(
            registry["models"]["deepseek/deepseek-v4-flash-0731"][
                "supports_parallel_tool_calls"
            ]
        )
        self.assertEqual(
            ["text", "image", "video"],
            registry["models"]["moonshotai/kimi-k3"]["openrouter_modalities"],
        )
        self.assertEqual(
            ["text", "image"],
            registry["models"]["moonshotai/kimi-k3"]["codex_modalities"],
        )

    def test_network_request_pins_active_zdr_provider_tags(self) -> None:
        class Response:
            status = 200
            headers = {"X-Generation-Id": "gen-test"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"model":"example/model"}'

        with mock.patch.object(
            doctor_module.urllib.request, "urlopen", return_value=Response()
        ) as urlopen:
            status, body, generation_id = doctor_module.request_model(
                "secret", "example/model", "high", ["provider-a", "provider-b"]
            )

        payload = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(200, status)
        self.assertEqual("example/model", body["model"])
        self.assertEqual("gen-test", generation_id)
        self.assertEqual(
            {"zdr": True, "order": ["provider-a", "provider-b"], "allow_fallbacks": False},
            payload["provider"],
        )

    def test_generation_metadata_retries_eventual_404(self) -> None:
        not_found = urllib.error.HTTPError(
            "https://openrouter.ai/api/v1/generation?id=gen-test", 404, "Not Found", {}, None
        )
        with (
            mock.patch.object(
                doctor_module,
                "authenticated_json",
                side_effect=[not_found, not_found, {"data": {"provider_name": "Example"}}],
            ) as request,
            mock.patch.object(doctor_module.time, "sleep") as sleep,
        ):
            metadata = doctor_module.generation_metadata("secret", "gen-test")
        self.assertEqual("Example", metadata["provider_name"])
        self.assertEqual(3, request.call_count)
        self.assertEqual([mock.call(2), mock.call(2)], sleep.call_args_list)

    def test_generation_metadata_does_not_retry_non_404(self) -> None:
        unauthorized = urllib.error.HTTPError(
            "https://openrouter.ai/api/v1/generation?id=gen-test", 401, "Unauthorized", {}, None
        )
        with (
            mock.patch.object(doctor_module, "authenticated_json", side_effect=unauthorized),
            mock.patch.object(doctor_module.time, "sleep") as sleep,
            self.assertRaises(urllib.error.HTTPError),
        ):
            doctor_module.generation_metadata("secret", "gen-test")
        sleep.assert_not_called()

    def test_doctor_template_is_a_thin_shim(self) -> None:
        """検査ロジックはモジュール側にあり、テンプレートは委譲するだけ。"""
        source = (ROOT / "portable/templates/codex-openrouter-doctor.py.in").read_text(
            encoding="utf-8"
        )
        self.assertIn("from codex_openrouter.doctor import run", source)
        self.assertLess(len(source.splitlines()), 60)
        for retired in ("app.asar", "adapter.json", "patched_asar", "OPENROUTER_APP"):
            self.assertNotIn(retired, source)

    def test_launcher_delegates_to_supervisor_and_never_touches_the_stock_app(self) -> None:
        """ランチャーは純正appを検証も改変もしない。事前処理はsupervisorが持つ。"""
        source = (ROOT / "portable/templates/codex-openrouter-app.zsh.in").read_text(
            encoding="utf-8"
        )
        self.assertIn("codex_openrouter.cli launch", source)
        self.assertIn("unset OPENROUTER_API_KEY", source)
        # クリックだけで最新runtimeが載るように、exec前に差分を反映する。
        self.assertIn("upgrade --if-needed", source)
        # pipe越しはblock bufferingされ、ログが空のままHUDにも行が届かない。
        self.assertIn("PYTHONUNBUFFERED=1", source)
        self.assertNotIn("/bin/ps -axo pid=,command=", source)
        # ASARパッチ方式の検証は全て不要になった。
        for retired in ("app.asar", "PATCH_MARKER", "patched_asar", "adapter.json", "codesign"):
            self.assertNotIn(retired, source)

    def test_upgrade_promotes_runtime_files_without_touching_the_stock_app(self) -> None:
        """案Dでは純正appを一切置換しない。upgradeの対象は自前のruntimeだけ。"""
        upgrade = (ROOT / "src/codex_openrouter/upgrade.py").read_text(encoding="utf-8")
        self.assertIn("(stage_support, paths.support_root)", upgrade)
        self.assertIn("paths.desktop_launcher", upgrade)
        # ASARパッチ方式の痕跡が残っていないこと。
        for retired in (
            "patched_asar_sha256",
            "adapters/index.json",
            "portable/manifest.json",
            "ChatGPT OpenRouter Candidate.app",
            "render_runtime.py",
        ):
            self.assertNotIn(retired, upgrade)

    def test_no_source_references_a_deleted_repository_path(self) -> None:
        """テンプレートとコードが参照するrepo内pathが実在すること。

        v0.2.0でASAR資産を消したとき、install.shが消えたファイルを参照したまま
        残っていた。CIは `zsh -n`（構文のみ）しか見ておらず検出できなかった。
        """
        pattern = re.compile(
            r"""["'](portable/[A-Za-z0-9_./-]+|models/[A-Za-z0-9_./-]+|"""
            r"""profiles/[A-Za-z0-9_./-]+|adapters/[A-Za-z0-9_./-]+|scripts/[A-Za-z0-9_./-]+)["']"""
        )
        missing = []
        for path in (ROOT / "src").rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            for match in pattern.finditer(path.read_text(encoding="utf-8")):
                referenced = match.group(1)
                if "@@" in referenced or "*" in referenced:
                    continue
                if not (ROOT / referenced).exists():
                    missing.append(f"{path.relative_to(ROOT)} -> {referenced}")
        self.assertEqual([], missing)

    def test_desktop_launcher_has_no_legacy_clone_references(self) -> None:
        """Swiftのentry pointも案Dへ追従していること。

        v0.2.0で旧clone appと旧homeを撤去したとき、このファイルだけが取り残されて
        削除済みpathを指したままになっていた。CIは swiftc のコンパイルしか見ないので
        落ちず、直前に足した path 実在テストは src/**/*.py しか走査していなかった。
        """
        swift = (ROOT / "portable/launcher/CodexOpenRouterLauncher.swift").read_text(
            encoding="utf-8"
        )
        for retired in ("ChatGPT OpenRouter.app", ".codex-openrouter"):
            self.assertNotIn(retired, swift)
        # pathの出所はPython側（UserPaths）。Swiftはplist経由で受け取るだけ。
        self.assertIn("CodexLauncherLog", swift)
        self.assertIn(
            "CodexLauncherLog", (ROOT / "portable/launcher/Info.plist").read_text(encoding="utf-8")
        )
        upgrade = (ROOT / "src/codex_openrouter/upgrade.py").read_text(encoding="utf-8")
        self.assertIn("CodexLauncherLog", upgrade)
        self.assertIn('state_dir / "logs/launcher.log"', upgrade)
        self.assertIn('run([str(paths.credential_helper), "status"])', upgrade)

    def test_progress_sentinels_match_between_swift_and_python(self) -> None:
        """HUDの出し入れは文字列の一致が全て。片方だけ変えると黙って出なくなる。"""
        swift = (ROOT / "portable/launcher/CodexOpenRouterLauncher.swift").read_text(
            encoding="utf-8"
        )
        for sentinel in (upgrade_module.STATUS_UPDATING, upgrade_module.STATUS_LAUNCHING):
            self.assertIn(f'"{sentinel}"', swift)

    def test_desktop_launcher_has_a_generated_project_icon(self) -> None:
        info = (ROOT / "portable/launcher/Info.plist").read_text(encoding="utf-8")
        upgrade = (ROOT / "src/codex_openrouter/upgrade.py").read_text(encoding="utf-8")
        self.assertIn("CFBundleIconFile", info)
        self.assertIn("AppIcon", info)
        self.assertIn("build_icon.zsh", upgrade)
        self.assertTrue((ROOT / "portable/launcher/CreateLauncherIcon.swift").is_file())


if __name__ == "__main__":
    unittest.main()
