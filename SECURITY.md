# Security Policy

## Supported version

Only the version in the root `VERSION` file is supported. It is experimental and currently supports Apple Silicon macOS only. ASAR patching and build adapters are not used.

## Report a vulnerability

Do not open a public issue for vulnerabilities, leaked credentials, guard bypasses, or managed-config corruption. Use GitHub Private Vulnerability Reporting in this repository's **Security > Advisories > Report a vulnerability** page.

Include the CLI version, macOS/ChatGPT build, the redacted `github-issue-diagnostics.tar.gz`, and reproduction steps. Never attach API keys, `auth.json`, Cookies, userData, ChatGPT.app, ASAR files, databases, or unredacted logs.

## Secret handling contract

- OpenRouter keys are accepted only through OAuth PKCE or hidden paste and stored in macOS Keychain.
- Keys are never accepted as CLI arguments and are not written to `.env`, configuration, logs, process arguments, or repository files.
- Only the local guard reads the OpenRouter key. Codex authenticates to the active guard with a separate launch-scoped token stored in a mode-0600 file.
- The guard validates that token before reading a request body and rejects every model outside the installed profile before retrieving the real key or contacting OpenRouter.
- `auth logout` removes only the local Keychain item after explicit confirmation. Server-side revocation remains a user action in OpenRouter.
- No telemetry or diagnostic bundle is uploaded automatically.

## Application and lifecycle boundary

The official `/Applications/ChatGPT.app` is never modified or cloned. The launcher temporarily adds managed catalog/provider blocks to the shared Codex configuration. When inactive, the persistent provider points to loopback port `0` and uses an authentication command that always fails. During normal cleanup the inactive stub is restored before the guard stops.

After `SIGKILL` or power loss, the active ephemeral port and launch token may remain until the next dedicated launch performs self-heal. The OpenRouter API key is still not exposed on loopback. A malicious process with the same user privileges may theoretically read the temporary token or capture a prompt during that interval; no always-running daemon is installed to close this residual local threat.
