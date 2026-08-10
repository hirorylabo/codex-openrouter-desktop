# codex-openrouter-desktop

> [!WARNING]
> **This is an unofficial, experimental workaround. It is not endorsed by or affiliated with OpenAI or OpenRouter.** The first release supports Apple Silicon macOS only. A ChatGPT.app update may stop it from working, and the software is provided without warranty. OpenAI, ChatGPT, Codex, OpenRouter, and model names are trademarks of their respective owners.

This CLI creates a dedicated local clone, `CODEX_HOME`, and Electron userData directory without modifying the official signed `/Applications/ChatGPT.app`, then connects the clone to an OpenRouter custom provider. The project does not distribute ChatGPT.app, ASAR files, API keys, cookies, history, userData, or logs.

[日本語](./README.md)

## Scope

- Apple Silicon macOS only; no Windows, Linux, Intel Mac, or Homebrew support.
- `v0.1.0` is a prerelease. The only known build is ChatGPT `26.803.41515` build `6321`.
- Unknown-build semantic candidates are best effort and never modify the stock app or promote without visual confirmation.
- OpenRouter usage charges are the user's responsibility. Network doctor and candidate canaries may incur a small charge.

## Verified download

The project deliberately does not use `curl | bash`.

```bash
mkdir codex-openrouter-download && cd codex-openrouter-download
gh release download v0.1.0 --repo hirorylabo/codex-openrouter-desktop \
  --pattern 'codex-openrouter-desktop-v0.1.0.tar.gz' \
  --pattern 'codex-openrouter-desktop-v0.1.0.spdx.json' \
  --pattern 'SHA256SUMS'
gh attestation verify codex-openrouter-desktop-v0.1.0.tar.gz \
  --repo hirorylabo/codex-openrouter-desktop
shasum -a 256 -c SHA256SUMS
tar -xzf codex-openrouter-desktop-v0.1.0.tar.gz
cd codex-openrouter-desktop-v0.1.0
```

## OpenRouter preparation

Install the official signed ChatGPT.app, Xcode Command Line Tools, Python 3.11+, and Node.js/npm. In OpenRouter, disable prompt training, public free endpoints, and the 1% data discount; enable Non-frontier ZDR; create a Guardrail whose exact model allowlist matches the selected profile; assign it to the OAuth-created key; and set a spend limit.

## Setup

```bash
./codex-openrouter check
./codex-openrouter setup --workspace "$HOME/Documents"
# Existing key, hidden paste:
./codex-openrouter setup --auth paste --workspace "$HOME/Documents"
```

OAuth uses PKCE S256 with a temporary random-port `127.0.0.1` callback. The API key is stored only in macOS Keychain under service `io.github.hirorylabo.codex-openrouter-desktop`. Codex obtains it through a credential helper; command-line key arguments, `.env`, shell profiles, configuration, and logs are not supported.

## Commands

```text
codex-openrouter check
codex-openrouter setup [--workspace PATH] [--profile default|FILE] [--auth oauth|paste]
codex-openrouter launch [PATH]
codex-openrouter doctor [--network] [--runtime] [--secret-scan]
codex-openrouter update
codex-openrouter rollback
codex-openrouter auth login|rotate|logout
```

Custom profiles may only select models already present in [`models/registry.json`](./models/registry.json). Before any application write, the CLI requires the API key's effective concrete model set to exactly match the profile.

Unknown builds are patched only in an isolated candidate using three single-match semantic anchors. Signature, ASAR integrity, exact App Server model inventory, every published reasoning effort, request-level ZDR, and the actual ZDR provider are checked before visual confirmation. Promotion requires typing `PROMOTE`; a failed post-promotion doctor automatically restores the prior app and runtime configuration.

See the Japanese README for the full operational details, [`SECURITY.md`](./SECURITY.md) for vulnerability reporting, and [`THIRD_PARTY_NOTICES.md`](./THIRD_PARTY_NOTICES.md) for third-party licensing.
