from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest import mock
import urllib.parse
from types import SimpleNamespace

from codex_openrouter import auth
from codex_openrouter import cli
from codex_openrouter import openrouter
from codex_openrouter.profile import ResolvedProfile
from codex_openrouter.profile import ProfileError, render_provider_mapping, resolve_profile


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "models/registry.json"


class ProfileTests(unittest.TestCase):
    def write_profile(self, document: dict) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temporary = tempfile.TemporaryDirectory()
        path = Path(temporary.name) / "profile.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return temporary, path

    def test_default_profile_resolves_exact_registry_subset(self) -> None:
        profile = resolve_profile(REGISTRY, ROOT / "profiles/default.json")
        self.assertEqual(5, len(profile.models))
        self.assertEqual("deepseek/deepseek-v4-pro", profile.default_model)
        mapping = render_provider_mapping(profile)
        self.assertEqual(set(profile.models), set(mapping["model_providers"]))

    def test_custom_profile_can_use_verified_subset(self) -> None:
        temporary, path = self.write_profile(
            {
                "schema_version": 1,
                "name": "small",
                "models": ["minimax/minimax-m3"],
                "default_model": "minimax/minimax-m3",
            }
        )
        self.addCleanup(temporary.cleanup)
        profile = resolve_profile(REGISTRY, path)
        self.assertIsNone(profile.default_effort)

    def test_duplicate_and_unknown_models_fail(self) -> None:
        for models in (
            ["minimax/minimax-m3", "minimax/minimax-m3"],
            ["unverified/model"],
        ):
            temporary, path = self.write_profile(
                {
                    "schema_version": 1,
                    "models": models,
                    "default_model": models[0],
                }
            )
            with self.subTest(models=models), self.assertRaises(ProfileError):
                resolve_profile(REGISTRY, path)
            temporary.cleanup()

    def test_guardrail_model_set_must_match_exactly_and_reject_qwen_aliases(self) -> None:
        key_document = {"data": {"is_management_key": False, "limit": 10}}
        expected = {"minimax/minimax-m3"}
        for ids in (
            ["minimax/minimax-m3", "unverified/model"],
            ["minimax/minimax-m3", "~qwen/qwen3.8-max"],
            [],
        ):
            models_document = {"data": [{"id": model} for model in ids]}
            with self.subTest(ids=ids), mock.patch.object(
                openrouter, "_get_json", side_effect=[key_document, models_document]
            ), self.assertRaises(openrouter.OpenRouterError):
                openrouter.validate_key_and_profile("not-logged", expected)


class AuthenticationTests(unittest.TestCase):
    def test_hidden_paste_accepts_key_without_echo(self) -> None:
        key = "sk-or-v1-" + "a" * 64
        with mock.patch("getpass.getpass", return_value=key) as prompt:
            self.assertEqual(key, auth.prompt_for_key())
        prompt.assert_called_once()

    def test_oauth_uses_real_loopback_callback_and_s256_exchange(self) -> None:
        key = "sk-or-v1-" + "b" * 64
        opened: dict[str, str] = {}

        def fake_open(command: list[str], **_kwargs: object) -> mock.Mock:
            url = command[-1]
            opened["url"] = url
            return mock.Mock(returncode=0)

        class Response:
            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps({"key": key}).encode()

        captured: dict = {}

        class LoopbackServer:
            server_port = 43123

            def __init__(self, address: tuple[str, int], handler: object):
                self.address = address
                self.handler = handler
                self.timeout = 0

            def handle_request(self) -> None:
                server_side, client_side = socket.socketpair()
                try:
                    client_side.sendall(
                        b"GET /callback?code=test-code HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
                    )
                    self.handler(server_side, ("127.0.0.1", 43123), self)
                    client_side.recv(4096)
                finally:
                    server_side.close()
                    client_side.close()

            def server_close(self) -> None:
                return None

        def fake_exchange(request: object, timeout: int) -> Response:
            self.assertEqual(30, timeout)
            captured.update(json.loads(request.data))
            return Response()

        with mock.patch("subprocess.run", side_effect=fake_open), mock.patch(
            "urllib.request.urlopen", side_effect=fake_exchange
        ):
            self.assertEqual(
                key,
                auth.oauth_key(timeout_seconds=2, _server_factory=LoopbackServer),
            )

        query = urllib.parse.parse_qs(urllib.parse.urlparse(opened["url"]).query)
        self.assertEqual(["S256"], query["code_challenge_method"])
        self.assertTrue(query["callback_url"][0].startswith("http://127.0.0.1:"))
        self.assertEqual("test-code", captured["code"])
        self.assertEqual("S256", captured["code_challenge_method"])
        self.assertNotEqual(captured["code_verifier"], query["code_challenge"][0])

    def test_credential_store_never_passes_secret_as_argument(self) -> None:
        key = "sk-or-v1-" + "c" * 64
        helper = Path("/tmp/fake-helper")
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)) as run_mock:
            auth.CredentialStore(helper).store(key)
        arguments = run_mock.call_args.args[0]
        self.assertEqual([str(helper), "store"], arguments)
        self.assertNotIn(key, arguments)
        self.assertEqual(key, run_mock.call_args.kwargs["input"])

    def test_auth_rotate_validates_then_replaces_keychain_item(self) -> None:
        key = "sk-or-v1-" + "d" * 64

        class Store:
            stored = ""

            def exists(self) -> bool:
                return True

            def store(self, value: str) -> None:
                self.stored = value

        store = Store()
        profile = ResolvedProfile(
            name="test",
            models=("minimax/minimax-m3",),
            default_model="minimax/minimax-m3",
            default_effort=None,
            registry={},
        )
        args = SimpleNamespace(auth_action="rotate", method="paste")
        with mock.patch.object(cli, "credential_store", return_value=(store, None)), mock.patch.object(
            cli, "resolved_profile", return_value=(Path("profile.json"), profile)
        ), mock.patch.object(cli, "obtain_key", return_value=key), mock.patch.object(
            cli, "validate_key_and_profile", return_value={"limit": 10}
        ) as validate:
            self.assertEqual(0, cli.auth_command(args))
        validate.assert_called_once_with(key, {"minimax/minimax-m3"})
        self.assertEqual(key, store.stored)


if __name__ == "__main__":
    unittest.main()
