# Codex OpenRouter Maintenance GUI 実装計画

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** 既存の `Codex OpenRouter.app`(Swift ランチャー)へ「メンテナンスモード」機能を追加し、デスクトップのショートカットから ChatGPT アカウントのサインアウト/切替(= native モードへの一時切替)と、picker 選択モデルのカスタマイズを GUI で行えるようにする。

**Architecture:** 新規 Swift ウィンドウ(`MaintenanceWindow.swift`)を既存ランチャーへ追加。バックエンドは既存 CLI への新規 `maintenance` サブコマンド群(Python)。config.toml の marker block 操作は確立済みの `configblock.py` を流用。state は `supervisor.State` を拡張。

**Tech Stack:** Swift/AppKit(既存 launcher app 内)、Python 3.11(codex_openrouter パッケージ)、TDD(unittest + scripts/run_unit_tests.py)

---

## 背景(実装者が知るべき事実)

1. **問題**： `model_provider="openrouter"` 起動中、ChatGPT.app はサードパーティ provider モードになりアカウント UI(ログアウト/サインイン)を隠す(app.asar 解析済み:`modelProviderName != null` → ログアウト項目が描画されない仕様)。メニューバーの「ログアウト」も無反応(実機確認済み)。auth.json 自体は無傷。
2. **解決方針**： provider/catalog の marker block と top-level key を一時的に native へ戻して app を再起動すれば、アプリ本来のサインイン UI が復活する。完了後は元へ戻す。
3. **既存資産**：
   - `src/codex_openrouter/configblock.py` — marker block 編集・`read_top_level`/`upsert_top_level`/`render_managed`/`edit`
   - `src/codex_openrouter/supervisor.py:70` — `State` dataclass(`saved_model`/`saved_provider` パターンあり)、`apply_config()`/`cleanup()`/`_restore_selection_text()`
   - `portable/launcher/app/` — Swift ランチャー(LauncherApp/LauncherPanel/ModelSettingsWindow/ProfileBridge)
   - `ProfileBridge.run()` — Swift→CLI 呼び出し口(`codex-openrouter profile show --json` 等)
   - デスクトップショートカットは既存： `~/Desktop/Codex OpenRouter.app`
4. **通常モード = ハイブリッド**: OR モデル + native モデル両方選択可(guard は OR モデルを bridge、native は deny して純正経路へフォールバック)。メンテナンスモードは「完全な native モード」へ一時的に寄せるだけ。

---

## Task 1: State に maintenance フラグを追加

**Objective:** メンテナンスモード中であることを永続化し、crash 後も検知できるようにする。

**Files:**
- Modify: `src/codex_openrouter/supervisor.py:70-87` (State dataclass)
- Test: `tests/test_supervisor.py`

**Step 1: Write failing test**

```python
def test_state_roundtrips_maintenance_flag(self):
    path = self.paths.state_dir / "supervisor.json"
    state = sup.State(active=False)
    state.maintenance_active = True
    state.save(path)
    loaded = sup.State.load(path)
    self.assertTrue(loaded.maintenance_active)
```

**Step 2:** Run `PATH="$HOME/.local/bin:$PATH" PYTHONPATH=src python3.11 -m unittest tests.test_supervisor -v`
Expected: FAIL — `__dataclass_fields__` に無く TypeError

**Step 3: Implement**

```python
# supervisor.py State dataclass へ追加:
    # メンテナンス(native切替)モード中。crash後もdoctorで検知できるよう永続化。
    maintenance_active: bool = False
```

**Step 4:** Run again. Expected: PASS

**Step 5: Commit**
```bash
git add src/codex_openrouter/supervisor.py tests/test_supervisor.py
git commit -m "feat: Stateにmaintenance_activeフラグを追加"
```

---

## Task 2: `maintenance switch-native` コマンド(TDD)

**Objective:** config.toml の catalog/provider block を退避し native モードへ切り替える Python コアを実装する。

**Files:**
- Create: `src/codex_openrouter/maintenance.py`
- Test: `tests/test_maintenance.py`

**Step 1: Write failing tests**

```python
import json, tempfile, unittest
from pathlib import Path
from codex_openrouter import maintenance
from codex_openrouter import configblock


class SwitchNativeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = self.root / "config.toml"
        text = 'model = "deepseek/deepseek-v4-flash"\n'
        text += 'model_provider = "openrouter"\n'
        text += "# >>> codex-openrouter:catalog >>>\n"
        text += 'model_catalog_json = "/tmp/cat.json"\n'
        text += "# <<< codex-openrouter:catalog <<<\n"
        text += "# >>> codex-openrouter:provider >>>\n"
        text += "[model_providers.openrouter]\n"
        text += "name = \"OpenRouter\"\n"
        text += "# <<< codex-openrouter:provider <<<\n"
        self.config.write_text(text)
        self.state_path = self.root / "state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_switch_native_strips_blocks_and_saves_state(self):
        doc = maintenance.switch_native(self.config, self.state_path,
                                        saved_model='model = "deepseek/deepseek-v4-flash"')
        text = self.config.read_text()
        assert "codex-openrouter" not in text          # marker block全消
        assert configblock.read_top_level(text, "model_provider") == "openai"
        assert configblock.read_top_level(text, "model") not in (
            "deepseek/deepseek-v4-flash",)
        state = json.loads(self.state_path.read_text())
        assert state["maintenance_active"] is True
        assert "catalog" in state["saved_blocks"]["catalog"]

    def test_switch_twice_raises(self):
        maintenance.switch_native(self.config, self.state_path, saved_model=None)
        with self.assertRaises(maintenance.MaintenanceError):
            maintenance.switch_native(self.config, self.state_path, saved_model=None)
```

**Step 2:** Run `PYTHONPATH=src python3.11 -m unittest tests.test_maintenance -v`
Expected: FAIL — ModuleNotFoundError

**Step 3: Implement `maintenance.py`**

```python
"""メンテナンスモード: 一時的にnative設定へ戻し、アプリ本来のサインインUIを使えるようにする。"""
from __future__ import annotations

import json
from pathlib import Path

from . import configblock

CATALOG_MARKER = "catalog"
PROVIDER_MARKER = "provider"


class MaintenanceError(RuntimeError):
    pass


def _extract_block(text: str, name: str) -> str | None:
    begin = f"# >>> codex-openrouter:{name} >>>"
    end = f"# <<< codex-openrouter:{name} <<<"
    if begin not in text:
        return None
    i = text.index(begin)
    j = text.index(end) + len(end)
    return text[i:j]


def _strip_block(text: str, name: str) -> str:
    block = _extract_block(text, name)
    if block is None:
        return text
    return text.replace(block, "").lstrip("\n")


def switch_native(config_path: Path, state_path: Path, *, saved_model: str | None) -> dict:
    state = _load(state_path)
    if state.get("maintenance_active"):
        raise MaintenanceError("既にメンテナンスモードです。account restoreを実行してください")

    def mutate(text: str) -> str:
        captured = {
            CATALOG_MARKER: _extract_block(text, CATALOG_MARKER),
            PROVIDER_MARKER: _extract_block(text, PROVIDER_MARKER),
        }
        state["saved_blocks"] = captured
        for name in (CATALOG_MARKER, PROVIDER_MARKER):
            text = _strip_block(text, name)
        current_provider = configblock.read_top_level(text, "model_provider")
        state["saved_provider"] = current_provider
        return configblock.upsert_top_level(text, "model_provider", '"openai"')

    new_text = configblock.edit(config_path, mutate)
    state["maintenance_active"] = True
    if saved_model is not None:
        state["saved_model"] = saved_model
    _save(state_path, state)
    return state


def restore(config_path: Path, state_path: Path) -> dict:
    state = _load(state_path)
    if not state.get("maintenance_active"):
        raise MaintenanceError("メンテナンスモードではありません")

    def mutate(text: str) -> str:
        blocks = state.get("saved_blocks") or {}
        tail = "\n".join(
            block for name in (PROVIDER_MARKER, CATALOG_MARKER)
            if (block := blocks.get(name)) is not None
        )
        return text.rstrip("\n") + "\n\n" + tail + "\n" if tail else text

    new_text = configblock.edit(config_path, mutate)
    state["maintenance_active"] = False
    _save(state_path, state)
    return new_text and state


def status(state_path: Path) -> dict:
    return _load(state_path)


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=1), encoding="utf-8")
```

※ `configblock.edit` のシグネチャは実装時に確認し、合わなければ薄い adapter を書くこと。marker 名は `configblock._validate_marker` と一致させること。

**Step 4:** Run again. Expected: PASS

**Step 5:** 全体 suite も通す: `scripts/run_unit_tests.py` → 386+ PASS

**Step 6: Commit**
```bash
git add src/codex_openrouter/maintenance.py tests/test_maintenance.py
git commit -m "feat: maintenance switch-native/restoreコア実装"
```

---

## Task 3: CLI wiring — `account status|switch-native|restore`

**Objective:** Task 2 のコアを `codex-openrouter account ...` サブコマンドとして公開する。

**Files:**
- Modify: `src/codex_openrouter/cli.py`(auth_command の隣、`~line 150`)
- Test: `tests/test_cli_account.py`

**Step 1: Write failing test**

```python
def test_account_status_reports_maintenance(self, capsys):
    # argparse 経由で account status を叩き、JSONが出ることを確認
    ...
```
(既存 `tests/test_cli*.py` の harness を流用すること)

**Step 2:** RED 確認 → **Step 3:** `cli.py` へ subparser 3 種と handler を追加。
出力は人間可読 + `--json` オプション(Swift 側が parse するため必須)。

**Step 4:** GREEN → **Step 5:** 全体 suite → **Step 6:**
```bash
git add src/codex_openrouter/cli.py tests/
git commit -m "feat: account status|switch-native|restore サブコマンド"
```

---

## Task 4: switch-native 時の app 再起動統合

**Objective:** switch-native が動いている ChatGPT.app を終了→再起動まで行う。

**Files:**
- Modify: `src/codex_openrouter/maintenance.py`
- Test: `tests/test_maintenance.py`

**設計：**
- `process_pids(paths.stock_app / "Contents/MacOS/ChatGPT")` で稼働検知(既存 util)
- 稼働中なら terminate→wait(既存 cleanup パターンと同じ)
- 再起動は `/usr/bin/open -a ChatGPT`(guard/supervisor なしの純正起動)
- LifecycleLock は取得しない(switch-native は一瞬で終わり、常駐しないため)。ただし `openrouter_is_running` 相当のチェックで OR ランチャー稼働中なら「先に終了してください」を返す

TDD 手順は Task 2 と同様(process 周りは mock)。

Commit: `git commit -m "feat: switch-native時のapp再起動統合"`

---

## Task 5: doctor へメンテナンス放置警告

**Objective:** maintenance_active のまま放置された場合、doctor/check が警告して復旧手順を表示する。

**Files:**
- Modify: `src/codex_openrouter/doctor.py`(check_tool_wire_build の隣 ~line 399)
- Test: `tests/test_doctor.py`

**テスト：** state に `maintenance_active: True` を入れて doctor を走らせ、
警告文「メンテナンスモードのままです。account restore を実行してください」が出ること。

Commit: `git commit -m "feat: doctorへメンテナンス放置警告を追加"`

---

## Task 6: Swift — MaintenanceWindow 追加

**Objective:** ランチャー GUI へ「Native アカウント管理…」ボタンと確認ダイアログを追加する。

**Files:**
- Create: `portable/launcher/app/MaintenanceWindow.swift`
- Modify: `portable/launcher/app/LauncherPanel.swift`(~line 24 のボタン列)
- Modify: `portable/launcher/app/LauncherApp.swift`(ボタン handler)
- Modify: `portable/launcher/app/ProfileBridge.swift`(`run(["account","status"])` 呼び出し)

**設計：**

```swift
// ProfileBridge へ追加
enum AccountBridge {
    struct Status: Decodable {
        let maintenanceActive: Bool
    }
    static func status() throws -> Status {
        // run(["account", "status", "--json"]) を呼んで decode
    }
    static func switchNative() throws { try runSimple(["account", "switch-native"]) }
    static func restore() throws { try runSimple(["account", "restore"]) }
}

// MaintenanceWindow.swift — 確認フロー:
// 1. 「ChatGPTアカウントのサインアウト/切替のため、nativeモードへ一時切り替えます。
//     OpenRouterモデルはpickerから消えます。続けますか?」確認ダイアログ
// 2. switch-native 実行 → app再起動待ち → 「サインイン後はこの画面の『復帰』を押してください」案内
// 3. 「復帰」ボタン → restore 実行
```

LauncherPanel のボタン列(settings/launch の横)へ `maintenanceButton = ActionButton(title: "アカウント管理…")` を追加。

**ビルド&目視確認：**
```bash
cd ~/agent/codex-openrouter-desktop && python3.11 ./codex-openrouter upgrade
# Desktopのappを起動してボタンが出ることを目視
```

Commit: `git commit -m "feat: ランチャーへアカウント管理ウィンドウを追加"`

---

## Task 7: picker モデルカスタマイズの統合確認

**Objective:** 「選択肢に出すモデルのカスタマイズ」がメンテナンス復帰後も正しく反映されることを確認する。既存 ModelSettingsWindow(models 設定)はあるため、本タスクは**統合テスト**。

**手順：**
1. 通常起動 → ModelSettings で models を編集・保存(既存機能)
2. メンテナンス switch-native → restore
3. `profile show --json` で編集した models が保持されていること
4. launch 後 picker に反映されていること(cua-driver 目視)

壊れていれば settings.py `_apply_locked` の profile digest フローとの相互作用を修正。

Commit: `git commit -m "test: メンテナンス復帰後のprofile保持を確認"`(修正が入ったら fix: )

---

## Task 8: 最終 Verification

- [ ] unit 全件 PASS(386+)・ruff pass
- [ ] `check`/`doctor` PASS(通常モード)
- [ ] 実機： switch-native → 歯車ポップアップに「Log out」出現(cua-driver スクショ)
- [ ] 実機： 別アカウントでサインイン → restore → OR モデル利用可
- [ ] 実機： native gpt モデルも新アカウントで利用可(limit 回避フロー成立)
- [ ] 実機： ModelSettings のモデルカスタマイズが復帰後も保持
- [ ] PR → CI 11/11 → merge → `upgrade` promote

---

## 未確定・要判断事項

1. switch-native 中の picker から OR モデルを消す(catalog block 外す)= 本計画の方針。
   「OR 表示のまま native 認証」の混在は避ける
2. Swift 側の文言は日本語で確定済み(「アカウント管理…」「復帰」)
3. restore を忘れて放置した場合の自動復帰はしない(doctor 警告のみ)— YAGNI
