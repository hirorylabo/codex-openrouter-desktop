# v0.1.1

Apple Silicon macOS向けの保守性改善prereleaseです。

- `VERSION`をreleaseの単一情報源にし、正規`vX.Y.Z` tagからasset名・SBOM・attestationを生成
- 既存installation向け`upgrade`を追加し、staging検証・transactional切替・全target自動rollbackを実装
- rebuild/installerをactive `adapter.json`とindexの完全一致で駆動
- main process検出をexact executable boundaryの共通実装へ統一
- upgrade、promotion、rollbackのsynthetic macOS E2EをCIへ追加
- Desktop launcherへOSS固有iconを追加し、setup/upgradeで署名付き再生成
- OpenRouter generation metadataのeventual 404 retryとprocess誤検出を修正

初回利用者向けのOAuth PKCE、Keychain、profile exact allowlist、ZDR canary、未知build candidate、SBOM・attestationは継続します。非公式・無保証の実験版です。
