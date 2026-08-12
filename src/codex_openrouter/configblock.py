"""`~/.codex/config.toml` を外科的に編集する。

このファイルは純正appが起動中に自分で書き換える（plugins, mcp_servers,
marketplaces, projects…）。全文レンダリングすると利用者の設定を消すので、
marker blockの追記/削除と、既存top-level keyの値差し替えだけを行う。

TOMLのtop-level keyは最初のtable headerより前に無ければならないので、
挿入位置はその制約に従う。
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import time

MARKER_PREFIX = "codex-openrouter"


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


def atomic_write(path: Path, text: str) -> None:
    """同一ディレクトリのtmpへ書いてからrename。modeは既存を引き継ぐ。"""
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    tmp = path.with_name(f".{path.name}.codex-openrouter-tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.chmod(mode)
    tmp.replace(path)


def edit(path: Path, mutate, *, attempts: int = 5) -> bool:
    """read-modify-writeを、appの並行書き込みを潰さないように行う。

    読み取り後にmtime_nsとsizeが変わっていたら書かずにやり直す。
    変更が無ければ書き込まずFalseを返す。
    """
    for attempt in range(attempts):
        try:
            before = path.stat()
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ConfigBlockError(f"config.tomlがありません: {path}") from exc
        updated = mutate(text)
        if updated == text:
            return False
        after = path.stat()
        if (after.st_mtime_ns, after.st_size) != (before.st_mtime_ns, before.st_size):
            time.sleep(0.05 * (attempt + 1))
            continue
        atomic_write(path, updated)
        return True
    raise ConfigBlockError("config.tomlが並行更新され続けているため書き込めませんでした")
