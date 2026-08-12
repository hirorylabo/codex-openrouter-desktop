"""選択されたmodelに `model_provider` を追随させる。

appはpickerの選択を `config/batchWrite` で `model` として書くが、
`model_provider` は書かない。providerは `thread/start` 時に束縛されるので、
**次のthreadが作られるまでに** 追随させれば足りる。

実測ではthread生成はモデル選択時ではなく最初の送信時で、その間隔は49秒あった。
したがって高頻度ポーリングは要らない。

追随に失敗しても誤routingにはならない。OR slugのままproviderがopenaiなら
chatgpt.comが400を返し、native slugのままproviderがopenrouterならguardが止める。
watcherが担うのは正しさではなく成功率。
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
import threading

from . import configblock

NATIVE_PROVIDER = "openai"
OPENROUTER_PROVIDER = "openrouter"
DEFAULT_POLL_SECONDS = 0.1


class Watcher:
    def __init__(
        self,
        config_path: Path,
        openrouter_models: Iterable[str],
        poll_seconds: float = DEFAULT_POLL_SECONDS,
    ):
        self.config_path = config_path
        self.openrouter_models = frozenset(openrouter_models)
        self.poll_seconds = poll_seconds

    def desired_provider(self, model: str | None) -> str:
        """未知slugはnative扱い。危険なのはnative本文がOpenRouterへ向かう方向だけ。"""
        if model is not None and model in self.openrouter_models:
            return OPENROUTER_PROVIDER
        return NATIVE_PROVIDER

    def _mutate(self, text: str) -> str:
        model = configblock.read_top_level(text, "model")
        desired = self.desired_provider(model)
        if configblock.read_top_level(text, "model_provider") == desired:
            return text
        return configblock.upsert_top_level(text, "model_provider", desired)

    def sync_once(self) -> bool:
        """1回だけ追随させる。書き換えたらTrue。

        appとの競合で起きる一過性の失敗ではloopを落とさない。落ちると追随が
        止まったことに誰も気づけず、OR選択が黙ってエラーになり続ける。
        """
        try:
            return configblock.edit(self.config_path, self._mutate)
        except (configblock.ConfigBlockError, OSError):
            return False

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.sync_once()
            stop.wait(self.poll_seconds)

    def start(self) -> tuple[threading.Thread, threading.Event]:
        stop = threading.Event()
        thread = threading.Thread(target=self.run, args=(stop,), daemon=True)
        thread.start()
        return thread, stop
