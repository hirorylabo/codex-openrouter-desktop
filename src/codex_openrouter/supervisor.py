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
import secrets
import signal
import subprocess
import threading

from . import catalog, configblock, guard as guard_module, watcher as watcher_module
from .app import AppError, UserPaths, stock_build_id
from .auth import CredentialStore
from .lifecycle import LifecycleLock
from .processes import process_pids
from .profile import ResolvedProfile, resolve_profile

CATALOG_BLOCK = "catalog"
PROVIDER_BLOCK = "provider"
NATIVE_FALLBACK_MODEL = "gpt-5.6-sol"
STATE_SCHEMA_VERSION = 2


class SupervisorError(RuntimeError):
    pass


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
        self.registry_path = registry_path
        self.port = port
        self.workspace = workspace
        registry = json.loads(registry_path.read_text(encoding="utf-8"))["models"]
        self.all_registry_models = frozenset(registry)
        if profile is None:
            profile_path = (
                paths.installed_profile
                if paths.installed_profile.is_file()
                else registry_path.parent.parent / "profiles/default.json"
            )
            profile = resolve_profile(registry_path, profile_path)
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
        """version/buildが変わったときだけcatalogを組み直す。

        ASAR hashは見ない。223MBのハッシュを毎回走らせないため。
        """
        version, build = stock_build_id(self.paths.stock_app)
        unchanged = (version, build) == (self.state.version, self.state.build)
        if unchanged and self.paths.composite_catalog.is_file() and not force:
            return False
        catalog.generate(
            self.paths.stock_codex,
            self.paths.shared_home,
            self.registry_path,
            self.paths.composite_catalog,
            model_ids=self.profile.models,
        )
        self.state.version, self.state.build = version, build
        self.state.save(self.state_path)
        return True

    # [4][6] guard ----------------------------------------------------------
    def start_guard(self) -> int:
        credential = CredentialStore(self.paths.credential_helper)
        instance = guard_module.Guard(
            allowed_models=self.profile.models,
            key_provider=credential.get,
            log_path=self.paths.guard_log,
            nonce=self.nonce,
            access_token=self.access_token,
        )
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
            if self.state.pending_default_model or (
                current_model in self.all_registry_models
                and current_model not in self.profile.models
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
            arguments.append(str(self.workspace))
        environment = {k: v for k, v in os.environ.items() if k != "OPENROUTER_API_KEY"}
        return subprocess.Popen(
            arguments,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=False,
        )

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
