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

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time

from . import catalog, configblock, guard as guard_module, watcher as watcher_module
from .app import AppError, UserPaths, stock_build_id
from .auth import CredentialStore
from .processes import process_pids

DEFAULT_PORT = 8791
CATALOG_BLOCK = "catalog"
PROVIDER_BLOCK = "provider"
NATIVE_FALLBACK_MODEL = "gpt-5.6-sol"


class SupervisorError(RuntimeError):
    pass


@dataclass
class State:
    """再起動やクラッシュをまたいで持ち越す情報。"""

    version: str | None = None
    build: str | None = None
    saved_model: str | None = None
    saved_provider: str | None = None
    active: bool = False
    extra: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "State":
        try:
            return cls(**json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, TypeError):
            return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(json.dumps(self.__dict__, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(path)


def provider_block_body(port: int, credential_helper: Path) -> str:
    """鍵そのものはconfigに書かない。Keychain helperのパスだけを置く。"""
    return "\n".join(
        [
            "[model_providers.openrouter]",
            'name = "OpenRouter"',
            f'base_url = "http://127.0.0.1:{port}/v1"',
            'wire_api = "responses"',
            "supports_websockets = false",
            "",
            "[model_providers.openrouter.auth]",
            f"command = {configblock.toml_string(str(credential_helper))}",
            'args = ["get"]',
            "timeout_ms = 10000",
            "refresh_interval_ms = 0",
        ]
    )


class Supervisor:
    def __init__(
        self,
        paths: UserPaths,
        registry_path: Path,
        port: int = DEFAULT_PORT,
        workspace: Path | None = None,
    ):
        self.paths = paths
        self.registry_path = registry_path
        self.port = port
        self.workspace = workspace
        self.registry_models = json.loads(registry_path.read_text(encoding="utf-8"))["models"]
        self.state_path = paths.state_dir / "supervisor.json"
        self.state = State.load(self.state_path)
        self.nonce = os.urandom(8).hex()
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
            return actions

        def mutate(text: str) -> str:
            return configblock.remove_block(text, CATALOG_BLOCK)

        try:
            if configblock.edit(config, mutate):
                actions.append("残っていたcatalog blockを除去しました")
        except configblock.ConfigBlockError:
            pass

        if self.state.active:
            self._restore_selection(actions)
            self.state.active = False
            self.state.save(self.state_path)
        return actions

    def _restore_selection(self, actions: list[str]) -> None:
        """OR選択のまま終わっていたら native 既定へ戻す。

        catalogを外した状態でmodelがOR slugのままだと、純正起動時に
        catalogに無いslugを指すことになる。
        """
        saved_model = self.state.saved_model
        saved_provider = self.state.saved_provider

        def mutate(text: str) -> str:
            current = configblock.read_top_level(text, "model")
            if current in self.registry_models:
                target = saved_model
                if target is None or target in self.registry_models:
                    target = NATIVE_FALLBACK_MODEL
                text = configblock.upsert_top_level(text, "model", target)
            if configblock.read_top_level(text, "model_provider") == "openrouter":
                text = configblock.upsert_top_level(
                    text, "model_provider", saved_provider or "openai"
                )
            return text

        try:
            if configblock.edit(self.paths.shared_config, mutate):
                actions.append("OpenRouter選択をnative既定へ戻しました")
        except configblock.ConfigBlockError:
            pass

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
        )
        self.state.version, self.state.build = version, build
        self.state.save(self.state_path)
        return True

    # [4][6] guard ----------------------------------------------------------
    def start_guard(self) -> int:
        if guard_module.health_ok(self.port, self.nonce):
            return self.port
        credential = CredentialStore(self.paths.credential_helper)
        instance = guard_module.Guard(
            allowed_models=self.registry_models,
            key_provider=credential.get,
            log_path=self.paths.guard_log,
            nonce=self.nonce,
        )
        try:
            self._server, actual = guard_module.serve(instance, port=self.port)
        except OSError as exc:
            raise SupervisorError(
                f"guardをport {self.port} で起動できません（別プロセスが使用中）: {exc}"
            ) from exc
        if not guard_module.health_ok(actual, self.nonce):
            raise SupervisorError(f"port {actual} に居るのが自分のguardではありません")
        return actual

    def start_watcher(self) -> None:
        instance = watcher_module.Watcher(self.paths.shared_config, self.registry_models)
        self._watcher_thread, self._watcher_stop = instance.start()

    # [5] config ------------------------------------------------------------
    def apply_config(self, port: int) -> None:
        config = self.paths.shared_config
        if not config.is_file():
            raise SupervisorError(f"{config} がありません。先に純正appを一度起動してください。")

        text = config.read_text(encoding="utf-8")
        self.state.saved_model = configblock.read_top_level(text, "model")
        self.state.saved_provider = configblock.read_top_level(text, "model_provider")
        self.state.active = True
        self.state.save(self.state_path)

        catalog_body = (
            f"model_catalog_json = {configblock.toml_string(str(self.paths.composite_catalog))}"
        )
        provider_body = provider_block_body(port, self.paths.credential_helper)

        def mutate(current: str) -> str:
            current = configblock.insert_block(
                current, CATALOG_BLOCK, catalog_body, top_level=True
            )
            return configblock.insert_block(
                current, PROVIDER_BLOCK, provider_body, top_level=False
            )

        configblock.edit(config, mutate)

    def ensure_provider_block(self, port: int) -> None:
        """B blockだけを永続化する。migrate と rollback から使う。"""
        body = provider_block_body(port, self.paths.credential_helper)
        configblock.edit(
            self.paths.shared_config,
            lambda text: configblock.insert_block(text, PROVIDER_BLOCK, body, top_level=False),
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
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

        config = self.paths.shared_config
        if config.is_file():
            try:
                if configblock.edit(
                    config, lambda text: configblock.remove_block(text, CATALOG_BLOCK)
                ):
                    actions.append("catalog blockを外しました（純正起動はvanillaに戻ります）")
            except configblock.ConfigBlockError:
                pass
            self._restore_selection(actions)

        self.state.active = False
        self.state.save(self.state_path)
        return actions

    # ---------------------------------------------------------------------
    def run(self) -> int:
        for message in self.self_heal():
            print(f"復旧: {message}")
        self.assert_stock_not_running()
        if self.refresh_catalog_if_needed():
            print(f"モデルカタログを再生成しました（build {self.state.build}）")

        port = self.start_guard()
        self.apply_config(port)
        self.start_watcher()

        installed: list[int] = []
        for name in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(name, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
                installed.append(name)
            except ValueError:
                pass

        process = None
        try:
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
