# Security Policy

## Supported version

Only the version in the root `VERSION` file is supported. It is experimental and supports only builds listed in `adapters/index.json`.

## Report a vulnerability

Do not open a public issue for vulnerabilities, leaked credentials, or a bypass that could modify the stock app. Use GitHub Private Vulnerability Reporting in this repository's **Security > Advisories > Report a vulnerability** page.

Include the CLI version, macOS/ChatGPT build, the redacted `github-issue-diagnostics.tar.gz`, and reproduction steps. Never attach API keys, `auth.json`, Cookies, userData, ChatGPT.app, ASAR files, databases, or unredacted logs.

## Secret handling contract

- OpenRouter keys are accepted only through OAuth PKCE or hidden paste and stored in macOS Keychain.
- Keys are never accepted as CLI arguments and are not written to `.env`, configuration, logs, process arguments, or repository files.
- `auth logout` removes only the local Keychain item after explicit confirmation. Server-side revocation remains a user action in OpenRouter.
- No telemetry or diagnostic bundle is uploaded automatically.

## Build boundary

The official `/Applications/ChatGPT.app` must retain a valid signature and the expected stock ASAR hash. All patching occurs in a clone. Unknown builds remain candidates until automated checks and explicit visual approval succeed.
