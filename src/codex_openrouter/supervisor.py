"""`Codex OpenRouter.app` の中身。事前処理 → 純正app起動 → 後始末。

純正 `/Applications/ChatGPT.app` は一切変更しない。ASARパッチもcloneも作らず、
`~/.codex/config.toml` にmarker blockを出し入れするだけで picker に
OpenRouter モデルを足す。

blockごとに寿命が違う。
  A catalog : ランチャー実行中のみ。終了時に外す（純正起動をvanillaに戻すため）
  B provider: 永続。外すとOR記録threadのresumeが `Model provider ... not found`
              でハードエラーになる
  C 選択状態: `model` / `model_provider` を終了時にnative既定へ戻す
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import plistlib
import secrets
import signal
import subprocess
import threading
import time

from . import (
    catalog,
    configblock,
    guard as guard_module,
    toolbridge,
    toolcompat,
    watcher as watcher_module,
)
from .app import AppError, UserPaths, stock_build_id
from .auth import CredentialStore
from .lifecycle import LifecycleLock
from .processes import process_pids
from .profile import ResolvedProfile, active_registry, installed_profile

CATALOG_BLOCK = "catalog"
PROVIDER_BLOCK = "provider"
# 起動直後のappがopen document eventを受け取れるまで待つ上限。無限には待たない。
WORKSPACE_DELIVERY_SECONDS = 45
# appは起動後に前回のprojectを非同期で復元する。早すぎるopenはその復元に
# 上書きされるため、落ち着くのを待ってから送り、最後のeventを勝たせる。
WORKSPACE_SETTLE_SECONDS = 5
WORKSPACE_DELIVERY_REPEATS = 2
NATIVE_FALLBACK_MODEL = "gpt-5.6-sol"
STATE_SCHEMA_VERSION = 4


class SupervisorError(RuntimeError):
    pass


def stock_bundle_identifier(stock_app: Path) -> str:
    """純正appのbundle id。値をハードコードせずInfo.plistから読む。"""
    try:
        document = plistlib.loads((stock_app / "Contents/Info.plist").read_bytes())
    except (OSError, plistlib.InvalidFileException) as exc:
        raise SupervisorError(f"公式ChatGPT.appのInfo.plistを読めません: {stock_app}") from exc
    identifier = document.get("CFBundleIdentifier")
    if not isinstance(identifier, str) or not identifier:
        raise SupervisorError(f"公式ChatGPT.appのbundle idを取得できません: {stock_app}")
    return identifier


@dataclass
class State:
    """再起動やクラッシュをまたいで持ち越す情報。"""

    schema_version: int = STATE_SCHEMA_VERSION
    version: str | None = None
    build: str | None = None
    saved_model: str | None = None
    saved_provider: str | None = None
    active: bool = False
    profile_digest: str | None = None
    pending_default_model: bool = False
    guard_port: int | None = None
    guard_nonce: str | None = None
    # 現在のcatalogがどのprofileから作られたか。build更新だけでなくprofile変更でも
    # 組み直すために持つ。
    catalog_profile_digest: str | None = None
    catalog_tool_digest: str | None = None

    @classmethod
    def load(cls, path: Path) -> "State":
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                return cls()
            known = {field for field in cls.__dataclass_fields__}
            values = {key: value for key, value in document.items() if key in known}
            values["schema_version"] = STATE_SCHEMA_VERSION
            return cls(**values)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(json.dumps(self.__dict__, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(path)


def provider_block_body(port: int, guard_token: Path | None = None) -> str:
    """実API keyを含まないactive providerまたは非接続stubを返す。"""
    if port == 0:
        command = "/usr/bin/false"
        args = "[]"
    elif guard_token is not None:
        command = "/bin/cat"
        args = f"[{configblock.toml_string(str(guard_token))}]"
    else:
        raise SupervisorError("active providerにはguard token pathが必要です")
    return "\n".join(
        [
            "[model_providers.openrouter]",
            'name = "OpenRouter"',
            f'base_url = "http://127.0.0.1:{port}/v1"',
            'wire_api = "responses"',
            "supports_websockets = false",
            "",
            "[model_providers.openrouter.auth]",
            f"command = {configblock.toml_string(command)}",
            f"args = {args}",
            "timeout_ms = 10000",
            "refresh_interval_ms = 0",
        ]
    )


class Supervisor:
    def __init__(
        self,
        paths: UserPaths,
        registry_path: Path,
        profile: ResolvedProfile | None = None,
        port: int = 0,
        workspace: Path | None = None,
    ):
        self.paths = paths
        self.tool_wire_builds = registry_path.parent / "tool-wire-builds.json"
        # 正本は導入済みregistryがあればそちら。同梱registryのまま読むと、
        # 設定画面で足したmodelがcatalog生成でKeyErrorになり、shutdown時の
        # native復帰でも「OpenRouterのmodelだと気づけない」状態になる。
        self.registry_path = active_registry(registry_path, paths)
        self.port = port
        self.workspace = workspace
        registry = json.loads(self.registry_path.read_text(encoding="utf-8"))["models"]
        self.all_registry_models = frozenset(registry)
        if profile is None:
            # 呼び出し側はふつう解決済みprofileを渡す。渡らなかった場合も
            # doctor・設定画面と同じ規則で選び直す。
            _selected, profile = installed_profile(registry_path, paths)
        self.profile = profile
        self.registry_models = profile.registry
        self.state_path = paths.supervisor_state
        self.state = State.load(self.state_path)
        if self.state.profile_digest != profile.digest:
            self.state.profile_digest = profile.digest
            self.state.pending_default_model = True
        self.nonce = secrets.token_hex(8)
        self.access_token = secrets.token_urlsafe(32)
        self._server = None
        self._watcher_stop: threading.Event | None = None
        self._watcher_thread: threading.Thread | None = None
        self._cleaned = False

    # [1] self-heal ---------------------------------------------------------
    def self_heal(self) -> list[str]:
        """前回の残骸を掃除する。強制終了された後でも純正がvanillaに戻るように。"""
        actions: list[str] = []
        config = self.paths.shared_config
        if not config.is_file():
            self.paths.guard_token.unlink(missing_ok=True)
            self.state.active = False
            self.state.guard_port = None
            self.state.guard_nonce = None
            self.state.save(self.state_path)
            return actions

        def mutate(text: str) -> str:
            updated = configblock.render_managed(
                text, provider_body=provider_block_body(0)
            )
            return self._restore_selection_text(updated)

        if configblock.edit(config, mutate):
            actions.append("managed configを非接続stubへ復旧しました")
        self.paths.guard_token.unlink(missing_ok=True)
        self.state.active = False
        self.state.guard_port = None
        self.state.guard_nonce = None
        self.state.save(self.state_path)
        return actions

    def _restore_selection_text(self, text: str) -> str:
        """OR選択のまま終わっていたら native 既定へ戻す。

        catalogを外した状態でmodelがOR slugのままだと、純正起動時に
        catalogに無いslugを指すことになる。
        """
        saved_model = self.state.saved_model
        saved_provider = self.state.saved_provider

        current = configblock.read_top_level(text, "model")
        if current in self.all_registry_models:
            target = saved_model
            if target is None or target in self.all_registry_models:
                target = NATIVE_FALLBACK_MODEL
            text = configblock.upsert_top_level(text, "model", target)
        if configblock.read_top_level(text, "model_provider") == "openrouter":
            text = configblock.upsert_top_level(
                text, "model_provider", saved_provider or "openai"
            )
        return text

    # [2] 排他 --------------------------------------------------------------
    def assert_stock_not_running(self) -> None:
        """`model_catalog_json` はconfig load時にしか効かないので後入れできない。"""
        executable = self.paths.stock_app / "Contents/MacOS/ChatGPT"
        if process_pids(executable):
            raise SupervisorError(
                "ChatGPT.appが既に起動しています。終了してから Codex OpenRouter を起動してください。"
            )

    # [3] update追従 --------------------------------------------------------
    def refresh_catalog_if_needed(self, force: bool = False) -> bool:
        """version/buildかprofileが変わったときだけcatalogを組み直す。

        ASAR hashは見ない。223MBのハッシュを毎回走らせないため。

        profile digestも見るのは、pickerとguard・watcher・doctorが必ず同じ集合を
        指すため。設定画面のapplyは旧catalogを消すのでここは素通りするが、
        `upgrade --profile` のようにcatalogを消さない経路でも自己回復する。
        """
        version, build = stock_build_id(self.paths.stock_app)
        tool_digest = toolcompat.compatibility_digest(
            self.profile.registry,
            self.paths.tool_compatibility,
            build,
        )
        unchanged = (version, build) == (self.state.version, self.state.build) and (
            self.state.catalog_profile_digest == self.profile.digest
        ) and (
            self.state.catalog_tool_digest == tool_digest
        )
        if unchanged and self.paths.composite_catalog.is_file() and not force:
            return False
        catalog.generate(
            self.paths.stock_codex,
            self.paths.shared_home,
            self.registry_path,
            self.paths.composite_catalog,
            model_ids=self.profile.models,
            snapshot=self.paths.clone_template_snapshot,
            build_id=(version, build),
            tool_compatibility=self.paths.tool_compatibility,
        )
        self.state.version, self.state.build = version, build
        self.state.catalog_profile_digest = self.profile.digest
        self.state.catalog_tool_digest = tool_digest
        self.state.save(self.state_path)
        return True

    # [4][6] guard ----------------------------------------------------------
    def start_guard(self) -> int:
        _version, build = stock_build_id(self.paths.stock_app)
        try:
            toolbridge.assert_supported_build(
                self.tool_wire_builds, build
            )
        except toolbridge.ToolBridgeError as exc:
            raise SupervisorError(str(exc)) from exc
        credential = CredentialStore(self.paths.credential_helper)
        instance = guard_module.Guard(
            allowed_models=self.profile.models,
            key_provider=credential.get,
            log_path=self.paths.guard_log,
            nonce=self.nonce,
            access_token=self.access_token,
            review_model=self.profile.default_model,
            zdr_models=[
                model
                for model, spec in self.profile.registry.items()
                if spec.get("zdr_supported", True)
            ],
        )
        self.guard = instance
        try:
            self._server, actual = guard_module.serve(instance, port=self.port)
        except OSError as exc:
            raise SupervisorError(
                f"guardをloopbackで起動できません: {exc}"
            ) from exc
        if not guard_module.health_ok(actual, self.nonce):
            raise SupervisorError(f"port {actual} に居るのが自分のguardではありません")
        self.paths.guard_token.parent.mkdir(parents=True, exist_ok=True)
        configblock.atomic_write(self.paths.guard_token, self.access_token)
        self.paths.guard_token.chmod(0o600)
        return actual

    def start_watcher(self) -> None:
        instance = watcher_module.Watcher(self.paths.shared_config, self.profile.models)
        self._watcher_thread, self._watcher_stop = instance.start()

    # [5] config ------------------------------------------------------------
    def apply_config(self, port: int) -> None:
        config = self.paths.shared_config
        if not config.is_file():
            raise SupervisorError(f"{config} がありません。先に純正appを一度起動してください。")

        catalog_body = (
            f"model_catalog_json = {configblock.toml_string(str(self.paths.composite_catalog))}"
        )
        provider_body = provider_block_body(port, self.paths.guard_token)
        captured: dict[str, str | None] = {}

        def mutate(current: str) -> str:
            captured["model"] = configblock.read_top_level(current, "model")
            captured["provider"] = configblock.read_top_level(current, "model_provider")
            current_model = captured["model"]
            # 単一model profileでは、専用起動のたびにそのmodelを選び直す。
            # 選択肢が1つしかないのに native のまま起動すると、利用者から見て
            # 「専用launcherで起動したのにOpenRouterへ行かない」だけになる。
            # 複数modelでは利用者のpicker選択を尊重し、pending契約のままにする。
            if (
                len(self.profile.models) == 1
                or self.state.pending_default_model
                or (
                    current_model in self.all_registry_models
                    and current_model not in self.profile.models
                )
            ):
                current = configblock.upsert_top_level(
                    current, "model", self.profile.default_model
                )
                current = configblock.upsert_top_level(
                    current, "model_provider", "openrouter"
                )
            return configblock.render_managed(
                current,
                catalog_body=catalog_body,
                provider_body=provider_body,
            )

        configblock.edit(config, mutate)
        self.state.saved_model = captured.get("model")
        self.state.saved_provider = captured.get("provider")
        self.state.active = True
        self.state.pending_default_model = False
        self.state.guard_port = port
        self.state.guard_nonce = self.nonce
        self.state.save(self.state_path)

    def ensure_inactive_config(self) -> None:
        """非稼働時のproviderを、外部接続不能なstubへ正規化する。"""
        configblock.edit(
            self.paths.shared_config,
            lambda text: configblock.render_managed(
                text, provider_body=provider_block_body(0)
            ),
        )

    # [7] 起動 --------------------------------------------------------------
    def launch(self) -> subprocess.Popen:
        executable = self.paths.stock_app / "Contents/MacOS/ChatGPT"
        if not executable.is_file():
            raise AppError(f"公式ChatGPT.appが見つかりません: {self.paths.stock_app}")
        arguments = [str(executable)]
        if self.workspace is not None:
            arguments.extend(("--open-project", str(self.workspace)))
        environment = {k: v for k, v in os.environ.items() if k != "OPENROUTER_API_KEY"}
        process = subprocess.Popen(
            arguments,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=False,
        )
        try:
            self.deliver_workspace()
        except Exception:
            # workspaceが届かないまま続けると、利用者から見て「dropしたfolderと
            # 違うprojectが開く」だけになる。黙って劣化させず、起動ごと止める。
            process.terminate()
            raise
        return process

    def deliver_workspace(self) -> None:
        """workspaceをLaunchServicesのopen document経路でも届ける。

        ChatGPT build 6849 は起動引数の `--open-project` を無視し、直前に開いて
        いたprojectを復元する。同じbuildでもopen document経路は効くため、起動後に
        改めて渡す。古いbuildでは引数側が効くので、そちらも従来どおり残している。
        """
        if self.workspace is None:
            return
        executable = self.paths.stock_app / "Contents/MacOS/ChatGPT"
        identifier = stock_bundle_identifier(self.paths.stock_app)
        deadline = time.monotonic() + WORKSPACE_DELIVERY_SECONDS
        # processとして見える前にopenを投げると、LaunchServicesが2つ目のinstanceを
        # 起こしうる。起動を確認してから送る。
        while not process_pids(executable):
            if time.monotonic() >= deadline:
                raise SupervisorError("純正appの起動を確認できず、workspaceを渡せませんでした")
            time.sleep(0.5)
        for _ in range(WORKSPACE_DELIVERY_REPEATS):
            time.sleep(WORKSPACE_SETTLE_SECONDS)
            self.send_workspace(identifier, deadline)

    def send_workspace(self, identifier: str, deadline: float) -> None:
        """open eventを1回届ける。失敗している間だけ期限まで再試行する。"""
        while True:
            result = subprocess.run(
                ["/usr/bin/open", "-b", identifier, str(self.workspace)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if result.returncode == 0:
                return
            if time.monotonic() >= deadline:
                raise SupervisorError(f"workspaceを純正appへ渡せませんでした: {self.workspace}")
            time.sleep(1)

    # [8] 後始末 ------------------------------------------------------------
    def cleanup(self) -> list[str]:
        if self._cleaned:
            return []
        self._cleaned = True
        actions: list[str] = []

        if self._watcher_stop is not None:
            self._watcher_stop.set()
        if self._watcher_thread is not None:
            self._watcher_thread.join(timeout=2)

        config = self.paths.shared_config
        config_error: Exception | None = None
        if config.is_file():
            try:
                if configblock.edit(
                    config,
                    lambda text: self._restore_selection_text(
                        configblock.render_managed(
                            text, provider_body=provider_block_body(0)
                        )
                    ),
                ):
                    actions.append("catalogを外しproviderを非接続stubへ戻しました")
            except (configblock.ConfigBlockError, OSError) as exc:
                config_error = exc

        if config_error is None:
            self.state.active = False
            self.state.guard_port = None
            self.state.guard_nonce = None
        self.state.save(self.state_path)
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        self.paths.guard_token.unlink(missing_ok=True)
        if config_error is not None:
            raise SupervisorError(f"managed configを安全に後始末できません: {config_error}")
        return actions

    # ---------------------------------------------------------------------
    def run(self) -> int:
        with LifecycleLock(self.paths):
            # 純正app稼働中の拒否はconfig/stateを1byteも変更しない。
            self.assert_stock_not_running()
            for message in self.self_heal():
                print(f"復旧: {message}")
            if self.refresh_catalog_if_needed():
                print(f"モデルカタログを再生成しました（build {self.state.build}）")

            installed: list[int] = []
            for name in (signal.SIGINT, signal.SIGTERM):
                try:
                    signal.signal(name, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
                    installed.append(name)
                except ValueError:
                    pass

            process = None
            try:
                port = self.start_guard()
                self.apply_config(port)
                self.start_watcher()
                process = self.launch()
                print(f"Codex OpenRouter: 起動しました (pid={process.pid}, guard=127.0.0.1:{port})")
                process.wait()
            except KeyboardInterrupt:
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
            finally:
                for message in self.cleanup():
                    print(f"後始末: {message}")
        return 0
