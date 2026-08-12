"""`~/.codex/config.toml` を外科的に編集する。

このファイルは純正appが起動中に自分で書き換える（plugins, mcp_servers,
marketplaces, projects…）。全文レンダリングすると利用者の設定を消すので、
marker blockの追記/削除と、既存top-level keyの値差し替えだけを行う。

TOMLのtop-level keyは最初のtable headerより前に無ければならないので、
挿入位置はその制約に従う。
"""

from __future__ import annotations

import contextlib
import itertools
import json
import os
from pathlib import Path
import re
import threading
import time
import tomllib

MARKER_PREFIX = "codex-openrouter"
_TMP_SEQUENCE = itertools.count()


class ConfigBlockError(RuntimeError):
    pass


def _begin(name: str) -> str:
    return f"# >>> {MARKER_PREFIX}:{name} >>>"


def _end(name: str) -> str:
    return f"# <<< {MARKER_PREFIX}:{name} <<<"


def _block_re(name: str) -> re.Pattern[str]:
    # 先行改行は取り込まない。挿入と除去を完全に対称にして、
    # 除去後に周囲の空行を触らずに済ませるため。
    return re.compile(
        re.escape(_begin(name)) + r"\n.*?" + re.escape(_end(name)) + r"[ \t]*\n?",
        re.DOTALL,
    )


def toml_string(value: str) -> str:
    """パス等をTOML basic stringにする。TOMLのescapeはJSONの部分集合で足りる。"""
    return json.dumps(value, ensure_ascii=False)


def first_table_index(text: str) -> int:
    """最初のtable header行の開始offset。無ければlen(text)。

    marker block内のtableは、blockごと動かす前提なので素通しで数える。
    """
    for match in re.finditer(r"(?m)^[ \t]*\[", text):
        return match.start()
    return len(text)


def has_block(text: str, name: str) -> bool:
    return _block_re(name).search(text) is not None


def remove_block(text: str, name: str) -> str:
    """blockを取り除く。無ければそのまま返す（冪等）。

    挿入と対称なので、利用者・appが書いた周囲の空行には一切触らない。
    """
    return _block_re(name).sub("", text, count=1)


def insert_block(text: str, name: str, body: str, *, top_level: bool) -> str:
    """blockを挿入する。既にあれば中身を差し替える（冪等）。

    top_level=True のkeyを含むblockは最初のtableより前へ置く。
    """
    body = body.strip("\n")
    rendered = f"{_begin(name)}\n{body}\n{_end(name)}\n"
    if has_block(text, name):
        return _block_re(name).sub(lambda _m: rendered, text, count=1)

    if top_level:
        index = first_table_index(text)
        head = text[:index]
        tail = text[index:]
        if head and not head.endswith("\n"):
            head += "\n"
        return head + rendered + tail

    if text and not text.endswith("\n"):
        text += "\n"
    return text + rendered


def read_top_level(text: str, key: str) -> str | None:
    """最初のtableより前にあるtop-level keyの生の値を返す。"""
    head = text[: first_table_index(text)]
    match = re.search(r"(?m)^[ \t]*" + re.escape(key) + r"[ \t]*=[ \t]*(.+?)[ \t]*$", head)
    if match is None:
        return None
    raw = match.group(1)
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    return raw


def upsert_top_level(text: str, key: str, value: str) -> str:
    """top-level keyの値を差し替える。無ければ最初のtableの直前に足す。"""
    rendered = f"{key} = {toml_string(value)}"
    index = first_table_index(text)
    head = text[:index]
    tail = text[index:]
    pattern = re.compile(r"(?m)^[ \t]*" + re.escape(key) + r"[ \t]*=[ \t]*.+?[ \t]*$")
    if pattern.search(head):
        return pattern.sub(lambda _m: rendered, head, count=1) + tail
    if head and not head.endswith("\n"):
        head += "\n"
    return head + rendered + "\n" + tail


def remove_top_level(text: str, key: str) -> str:
    index = first_table_index(text)
    head = text[:index]
    tail = text[index:]
    pattern = re.compile(r"(?m)^[ \t]*" + re.escape(key) + r"[ \t]*=[ \t]*.*$\n?")
    return pattern.sub("", head, count=1) + tail


def _validate_toml(text: str) -> dict:
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigBlockError(f"config.tomlが不正です: {exc}") from exc
    if not isinstance(document, dict):
        raise ConfigBlockError("config.tomlのトップレベルがtableではありません")
    return document


def _validate_marker(text: str, name: str) -> None:
    begins = text.count(_begin(name))
    ends = text.count(_end(name))
    if begins != ends or begins not in (0, 1):
        raise ConfigBlockError(f"managed {name} markerが重複または破損しています")
    if begins == 1 and len(_block_re(name).findall(text)) != 1:
        raise ConfigBlockError(f"managed {name} blockを安全に解釈できません")


def render_managed(
    text: str,
    *,
    provider_body: str,
    catalog_body: str | None = None,
) -> str:
    """managed catalog/providerを非重複の1回変換で組み直す。

    marker外の同名設定は利用者所有とみなし、推測で採用・上書きしない。
    旧実装でcatalog markerがprovider table内へ入ったconfigも、両blockを一度
    除去してから正しい位置へ再構築することで移行する。
    """
    _validate_toml(text)
    for name in ("catalog", "provider"):
        _validate_marker(text, name)

    unmanaged = remove_block(remove_block(text, "catalog"), "provider")
    document = _validate_toml(unmanaged)
    if "model_catalog_json" in document:
        raise ConfigBlockError(
            "marker外のmodel_catalog_jsonがあります。既存設定を変更せず停止します"
        )
    providers = document.get("model_providers")
    if isinstance(providers, dict) and "openrouter" in providers:
        raise ConfigBlockError(
            "marker外のmodel_providers.openrouterがあります。既存設定を変更せず停止します"
        )

    updated = unmanaged
    if catalog_body is not None:
        updated = insert_block(updated, "catalog", catalog_body, top_level=True)
    updated = insert_block(updated, "provider", provider_body, top_level=False)
    _validate_toml(updated)
    return updated


@contextlib.contextmanager
def _exclusive_lock(path: Path, timeout: float = 5.0):
    """自前のwriter同士を直列化する。

    純正appはこのlockを取らないので、appとの競合窓が完全に消えるわけではない。
    ただしlock保持中の処理は stat・read・write の数syscallまで縮むうえ、appが
    configを書くのは利用者がモデルを変えた瞬間だけなので実質的に衝突しない。
    """
    lock_path = path.with_name(f".{path.name}.codex-openrouter.lock")
    deadline = time.monotonic() + timeout
    handle = None
    while handle is None:
        try:
            handle = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if time.monotonic() > deadline:
                # 保持者が死んだ可能性がある。奪って進む。
                lock_path.unlink(missing_ok=True)
                deadline = time.monotonic() + timeout
            time.sleep(0.005)
        except OSError as exc:
            raise ConfigBlockError(f"lockを取得できません: {exc}") from exc
    try:
        yield
    finally:
        os.close(handle)
        lock_path.unlink(missing_ok=True)


def stage(path: Path, text: str) -> Path:
    """内容をtmpへ書き出すだけ。commitはまだしない。

    tmp名はwriterごとに一意にする。固定名にすると、watcherとappのように
    書き手が複数居るときに同じtmpを奪い合い、renameがENOENTで落ちる。
    """
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    unique = f"{os.getpid()}.{threading.get_ident()}.{next(_TMP_SEQUENCE)}"
    tmp = path.with_name(f".{path.name}.codex-openrouter-{unique}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.chmod(mode)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return tmp


def atomic_write(path: Path, text: str) -> None:
    """同一ディレクトリのtmpへ書いてからrename。modeは既存を引き継ぐ。"""
    tmp = stage(path, text)
    try:
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def edit(path: Path, mutate, *, attempts: int = 5) -> bool:
    """read-modify-writeを、appの並行書き込みを潰さないように行う。

    `mutate` はtextの純粋関数で冪等であること。**必ず書き込み直前に読み直して
    mutateをやり直す。** 古い全文を書き戻すと、その間にappが書いた `model` を
    巻き戻してしまう。巻き戻った状態は(model=native, provider=openai)のように
    それ自体は整合するので、以後のtickでは検知できず利用者の選択が失われる。

    既知の残存リスク: 純正appはこのlockを取らないので、read-modify-writeが
    真に同時に始まると失われる更新が理論上ありうる。検証後に残る操作を rename
    1回だけに縮めてあり、実運用条件（watcherが定常状態、appの書き込みは人間操作
    起点で非同期）では計測上ゼロ。両者が同一マイクロ秒で開始する人工条件でのみ
    再現する。失っても routing は安全側のままで、誤送信にはならない。

    変更が無ければ書き込まずFalseを返す。
    """
    def snapshot() -> tuple[tuple[int, int], str]:
        try:
            stat = path.stat()
            return (stat.st_mtime_ns, stat.st_size), path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ConfigBlockError(f"config.tomlがありません: {path}") from exc

    for attempt in range(attempts):
        _stamp, text = snapshot()
        if mutate(text) == text:
            return False

        # 自前のwriter同士（supervisorとwatcher）を直列化したうえで、
        # 読み直し → mutate再適用 → 検証 → 書き込み を最短で行う。
        with _exclusive_lock(path):
            stamp, fresh = snapshot()
            updated = mutate(fresh)
            if updated == fresh:
                return False
            # tmpの書き出しは検証より前に済ませる。検証のあとに残る操作を
            # rename 1回だけにして、他のwriterに割り込まれる窓を最小化する。
            tmp = stage(path, updated)
            current = path.stat()
            if (current.st_mtime_ns, current.st_size) != stamp:
                tmp.unlink(missing_ok=True)
                time.sleep(0.02 * (attempt + 1))
                continue
            try:
                tmp.replace(path)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
            return True
    raise ConfigBlockError("config.tomlが並行更新され続けているため書き込めませんでした")
