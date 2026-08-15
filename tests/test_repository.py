from __future__ import annotations

import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock
import urllib.error


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER_SOURCES = ROOT / "portable/launcher/app"


from codex_openrouter import doctor as doctor_module
from codex_openrouter import upgrade as upgrade_module
from scripts import macos_live_e2e as live_e2e


def launcher_swift() -> str:
    """ランチャーappのSwift全文。ファイル分割で検査が素通りしないよう束ねて見る。"""
    sources = sorted(LAUNCHER_SOURCES.glob("*.swift"))
    assert sources, f"ランチャーのSwift sourceがありません: {LAUNCHER_SOURCES}"
    return "\n".join(path.read_text(encoding="utf-8") for path in sources)


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
        self.assertIn('pin_python_shebang(stage_bin / "codex-openrouter", python)', upgrade)
        self.assertIn('pin_python_shebang(stage_bin / "codex-openrouter-doctor", python)', upgrade)
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
        swift = launcher_swift()
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
        swift = launcher_swift()
        for sentinel in (upgrade_module.STATUS_UPDATING, upgrade_module.STATUS_LAUNCHING):
            self.assertIn(f'"{sentinel}"', swift)

    def test_build_and_ci_compile_the_whole_launcher_source_directory(self) -> None:
        """ビルド側とCIが同じディレクトリを丸ごと見ること。

        ファイル名を個別に列挙すると、片方だけ増減して黙って対象から漏れる。
        `main.swift` が1つだけあることも見る。Swiftはtop-level codeをそこにしか
        許さないので、2つあるとリンクが壊れる。
        """
        upgrade = (ROOT / "src/codex_openrouter/upgrade.py").read_text(encoding="utf-8")
        self.assertIn('"portable/launcher/app"', upgrade)
        self.assertIn('glob("*.swift")', upgrade)
        self.assertIn("*launcher_sources(source_root)", upgrade)
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn("portable/launcher/app/*.swift", workflow)
        self.assertTrue((LAUNCHER_SOURCES / "main.swift").is_file())
        top_level = [
            path.name
            for path in LAUNCHER_SOURCES.glob("*.swift")
            if "NSApplication.shared" in path.read_text(encoding="utf-8")
            and "application.run()" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(["main.swift"], top_level)

    def test_launcher_is_a_regular_app_with_a_settings_entry(self) -> None:
        """管理ランチャーはDockとAppメニューを持つ通常app。常駐daemonにはしない。"""
        info = (ROOT / "portable/launcher/Info.plist").read_text(encoding="utf-8")
        self.assertNotIn("LSUIElement", info)
        swift = launcher_swift()
        self.assertIn("NSApplication.shared.mainMenu = mainMenu", swift)
        self.assertIn('withTitle: "設定…", action: #selector(openSettings(_:)), keyEquivalent: ","', swift)
        # 画面を閉じたら終わる。OpenRouterセッション終了でも終わる。
        self.assertIn("func applicationShouldTerminateAfterLastWindowClosed", swift)
        self.assertIn("NSApplication.shared.terminate(nil)", swift)

    def test_launcher_shows_the_panel_before_starting_chatgpt(self) -> None:
        """起動もfolder dropも管理画面を出すだけ。ChatGPTはボタンを押すまで動かない。"""
        swift = launcher_swift()
        self.assertIn("adopt(workspace: path)", swift)
        self.assertIn("func startOpenRouter()", swift)
        self.assertIn('ActionButton(title: "OpenRouterで起動"', swift)
        self.assertIn('ActionButton(title: "モデル設定…"', swift)
        # 旧実装は起動直後に自動でhelperを起動していた。
        self.assertNotIn("if !self.receivedWorkspace", swift)

    def test_launcher_delegates_every_profile_decision_to_the_cli(self) -> None:
        """profile・Keychain・Guardrailの判断をSwiftへ複製しない。"""
        swift = launcher_swift()
        self.assertIn('["profile", "show", "--json"]', swift)
        self.assertIn('["profile", "apply", "--stdin-json"]', swift)
        for duplicated in (
            "sk-or-",
            "registry.json",
            "profiles/default.json",
            "SecItem",
            "kSecClass",
            "openrouter.ai/api",
            "supervisor.json",
        ):
            self.assertNotIn(duplicated, swift)

    def test_settings_window_requires_one_model_and_an_explicit_default(self) -> None:
        """空集合と「既定を外したまま保存」をUI側でも止める。"""
        swift = (LAUNCHER_SOURCES / "ModelSettingsWindow.swift").read_text(encoding="utf-8")
        self.assertIn("最低1モデルを選択してください。", swift)
        self.assertIn("既定モデルを選び直してください。", swift)
        self.assertIn("ChatGPT終了後に変更できます。", swift)
        self.assertIn("次回のOpenRouter起動から反映されます。", swift)
        self.assertIn('ActionButton(title: "OpenRouter Guardrailを開く"', swift)
        self.assertIn('ActionButton(title: "検証して保存"', swift)
        # 保存可否・案内文・保存payloadが同じ判定を見ること。別々に持つと
        # 「理由が出ないまま保存ボタンだけ無効」になる。式そのものではなく、
        # 3者が同じ入口を通ることだけを見る。
        self.assertIn("private var resolvedDefault: String? {", swift)
        self.assertIn("if resolvedDefault == nil {", swift)
        self.assertIn("let defaultModel = resolvedDefault", swift)
        # 既定を外したら黙って他へ寄せない。
        self.assertIn("defaultModel = nil", swift)

    def test_adding_a_non_zdr_model_is_confirmed_rather_than_silent(self) -> None:
        """ZDRなしの追加は既定の安全性を下げる。黙って通す経路を作らない。

        Pythonの `guard.prepare` はregistryの `zdr_supported` に従うだけなので、
        「そのmodelでZDRを外してよい」と決めているのは実質この画面になる。
        """
        swift = (LAUNCHER_SOURCES / "ModelSettingsWindow.swift").read_text(encoding="utf-8")
        self.assertIn("confirmNonZdr", swift)
        self.assertIn("providerに保持される可能性", swift)
        # 追加は確認の応答を受けてから。分岐なしにselectedへ入れない。
        self.assertIn("guard let self, response == .alertFirstButtonReturn else { return }", swift)
        # 一覧の既定は安全側。
        table = (LAUNCHER_SOURCES / "ModelCatalogTable.swift").read_text(encoding="utf-8")
        self.assertIn("var zdrOnly = true", table)
        self.assertIn('zdrOnly.state = .on', swift)

    def test_settings_window_labels_usage_as_tokens_not_connections(self) -> None:
        """OpenRouterが公開しているのはトークン総数で、接続数ではない。

        「接続数」と名乗ると、利用者は別の指標だと思って読む。
        """
        swift = (LAUNCHER_SOURCES / "ModelSettingsWindow.swift").read_text(encoding="utf-8")
        table = (LAUNCHER_SOURCES / "ModelCatalogTable.swift").read_text(encoding="utf-8")
        self.assertIn("7dトークン", table)
        self.assertNotIn("接続数", swift)
        self.assertNotIn("接続数", table)
        # トップ50圏外は0ではなくデータなし。
        self.assertIn('return "—"', table)

    def test_model_catalog_sorts_from_clickable_column_headers(self) -> None:
        """列headerを再クリックするとAppKit標準で降順・昇順が反転する。"""
        settings = (LAUNCHER_SOURCES / "ModelSettingsWindow.swift").read_text(
            encoding="utf-8"
        )
        table = (LAUNCHER_SOURCES / "ModelCatalogTable.swift").read_text(encoding="utf-8")
        self.assertNotIn("sortPopUp", settings)
        self.assertIn("列名をクリックして並び替え", settings)
        self.assertIn("column.sortDescriptorPrototype", table)
        self.assertIn("sortDescriptorsDidChange", table)
        # 初期表示と別列の最初のクリックはいずれも降順から始める。
        self.assertIn("sortDescriptor(for: .released, ascending: false)", table)
        self.assertIn("sortDescriptor(for: sortField, ascending: false)", table)
        # 未取得値は昇降順に関係なく末尾へ置く。
        self.assertIn('case (nil, _):\n            return false', table)

    def test_desktop_launcher_gracefully_hands_off_from_the_exact_stock_app(self) -> None:
        """純正起動中は確認後に通常終了を待ち、強制終了せずhelperへ渡す。"""
        swift = launcher_swift()
        self.assertIn('URL(fileURLWithPath: "/Applications/ChatGPT.app")', swift)
        self.assertIn("$0.bundleURL?.standardizedFileURL == stockAppURL", swift)
        self.assertIn("終了してOpenRouterモードへ切り替える", swift)
        self.assertIn("application.terminate()", swift)
        self.assertIn("Date().addingTimeInterval(30)", swift)
        self.assertIn("waitForStockTermination", swift)
        self.assertIn("ignoredStockProcessIdentifiers", swift)
        self.assertNotIn("forceTerminate()", swift)

    def test_installed_e2e_detects_lifecycle_state_without_copying_markers(self) -> None:
        """実launcher E2Eはmarker表記変更でactiveを見失ってはいけない。"""
        source = (ROOT / "scripts/macos_installed_e2e.zsh").read_text(encoding="utf-8")
        self.assertIn("state_field active", source)
        self.assertIn("state_field guard_port", source)
        self.assertIn('"$mode" == "600"', source)
        self.assertIn("run_cycle 1", source)
        self.assertIn("run_cycle 2", source)
        self.assertIn("wait_launcher_exit", source)
        self.assertIn("codex_openrouter.processes", source)
        self.assertNotIn("pgrep", source)
        self.assertNotIn("codex-openrouter:catalog", source)
        # 管理画面が入り、クリックだけでは起動しなくなった。
        self.assertIn("profile show --json", source)
        self.assertIn("OpenRouterで起動", source)
        self.assertIn("manual_checklist", source)

    def test_live_e2e_removes_auth_copy_even_when_cleanup_raises(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            base = Path(directory) / "e2e"
            auth_copy = base / ".codex/auth.json"

            def fail_after_copy() -> int:
                auth_copy.parent.mkdir(parents=True)
                auth_copy.write_text("sensitive-copy", encoding="utf-8")
                raise RuntimeError("cleanup failed")

            with mock.patch.object(live_e2e, "BASE", base), \
                 mock.patch.object(live_e2e, "_run_main", side_effect=fail_after_copy), \
                 self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                live_e2e.main()
            self.assertFalse(auth_copy.exists())

    def test_desktop_launcher_has_a_generated_project_icon(self) -> None:
        info = (ROOT / "portable/launcher/Info.plist").read_text(encoding="utf-8")
        upgrade = (ROOT / "src/codex_openrouter/upgrade.py").read_text(encoding="utf-8")
        self.assertIn("CFBundleIconFile", info)
        self.assertIn("AppIcon", info)
        self.assertIn("build_icon.zsh", upgrade)
        self.assertTrue((ROOT / "portable/launcher/CreateLauncherIcon.swift").is_file())


if __name__ == "__main__":
    unittest.main()
