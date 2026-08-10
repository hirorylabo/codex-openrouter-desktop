from __future__ import annotations

import base64
import getpass
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import secrets
import subprocess
import tempfile
from typing import Callable
import urllib.error
import urllib.parse
import urllib.request


OAUTH_AUTHORIZE = "https://openrouter.ai/auth"
OAUTH_EXCHANGE = "https://openrouter.ai/api/v1/auth/keys"


class AuthenticationError(RuntimeError):
    pass


class CredentialStore:
    def __init__(self, helper: Path):
        self.helper = helper

    @classmethod
    def compile(cls, source: Path, output: Path) -> "CredentialStore":
        output.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["/usr/bin/xcrun", "swiftc", str(source), "-o", str(output)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            raise AuthenticationError("Keychain helperをコンパイルできません")
        output.chmod(0o755)
        return cls(output)

    def exists(self) -> bool:
        return subprocess.run(
            [str(self.helper), "status"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode == 0

    def get(self) -> str:
        result = subprocess.run(
            [str(self.helper), "get"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0 or not result.stdout.startswith("sk-or-"):
            raise AuthenticationError("macOS KeychainからOpenRouter API keyを取得できません")
        return result.stdout

    def store(self, key: str) -> None:
        result = subprocess.run(
            [str(self.helper), "store"],
            input=key,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            raise AuthenticationError("OpenRouter API keyをmacOS Keychainへ保存できません")

    def delete(self) -> None:
        result = subprocess.run(
            [str(self.helper), "delete"],
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            raise AuthenticationError("macOS Keychainのcredentialを削除できません")


def _valid_key(key: str) -> bool:
    return key.startswith("sk-or-") and len(key) >= 32 and not any(ch.isspace() for ch in key)


def prompt_for_key() -> str:
    key = getpass.getpass("OpenRouter API key（入力は表示されません）: ").strip()
    if not _valid_key(key):
        raise AuthenticationError("OpenRouter API keyの形式が不正です")
    return key


def oauth_key(
    timeout_seconds: int = 600,
    *,
    _server_factory: Callable[..., HTTPServer] = HTTPServer,
) -> str:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    received: dict[str, str] = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            code = query.get("code", [""])[0]
            if parsed.path != "/callback" or not code:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Invalid OpenRouter callback")
                return
            received["code"] = code
            body = (
                "<!doctype html><meta charset='utf-8'>"
                "<title>Codex OpenRouter Desktop</title>"
                "<p>OpenRouter authorization completed. You can close this tab.</p>"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = _server_factory(("127.0.0.1", 0), CallbackHandler)
    server.timeout = timeout_seconds
    callback = f"http://127.0.0.1:{server.server_port}/callback"
    authorization_url = OAUTH_AUTHORIZE + "?" + urllib.parse.urlencode(
        {
            "callback_url": callback,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "key_label": "codex-openrouter-desktop",
        }
    )
    print("OpenRouterの認証画面を開きます。10分以内に承認してください。")
    print(f"自動で開かない場合: {authorization_url}")
    subprocess.run(
        ["/usr/bin/open", authorization_url],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    server.handle_request()
    server.server_close()
    code = received.get("code")
    if not code:
        raise AuthenticationError("OpenRouter OAuth callbackが時間内に完了しませんでした")

    payload = json.dumps(
        {
            "code": code,
            "code_verifier": verifier,
            "code_challenge_method": "S256",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        OAUTH_EXCHANGE,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            document = json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise AuthenticationError(
            f"OpenRouter authorization codeをAPI keyへ交換できません: {type(exc).__name__}"
        ) from exc
    key = document.get("key") if isinstance(document, dict) else None
    if not isinstance(key, str) or not _valid_key(key):
        raise AuthenticationError("OpenRouter OAuth responseに有効なAPI keyがありません")
    return key


def temporary_store(source: Path) -> tuple[CredentialStore, tempfile.TemporaryDirectory[str]]:
    temporary = tempfile.TemporaryDirectory(prefix="codex-openrouter-credential-")
    helper = Path(temporary.name) / "codex-openrouter-credential"
    return CredentialStore.compile(source, helper), temporary


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()
