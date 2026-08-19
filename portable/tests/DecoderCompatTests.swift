import Foundation

/// 新しいtool互換fieldを含むCLI出力と、field追加前のv0.2.1出力を同じlauncherが
/// どちらも読めることを固定する小さなCI harness。
private var failures: [String] = []

private func check(_ condition: Bool, _ message: String) {
    if !condition { failures.append(message) }
}

private func fixture(_ name: String) -> Data {
    let root = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .deletingLastPathComponent()
    let path = root.appendingPathComponent("tests/fixtures/\(name)")
    guard let data = try? Data(contentsOf: path) else {
        FileHandle.standardError.write(Data("missing fixture: \(path.path)\n".utf8))
        exit(2)
    }
    return data
}

private func decode<T: Decodable>(_ type: T.Type, _ name: String) -> T? {
    let decoder = JSONDecoder()
    decoder.keyDecodingStrategy = .convertFromSnakeCase
    do {
        return try decoder.decode(type, from: fixture(name))
    } catch {
        failures.append("\(name): \(error)")
        return nil
    }
}

@main
struct DecoderCompatTests {
    static func main() {
        if let snapshot = decode(ProfileBridge.Snapshot.self, "launcher-profile-show.json") {
            let model = snapshot.available.first
            check(model?.toolSupport == "partial", "profile showのtool状態を読めません")
            check(model?.toolVerifiedAt != nil, "profile showの検査時刻を読めません")
            check(model?.toolProvider == "DeepInfra", "profile showの検証providerを読めません")
            check(model?.toolContractVersion == 2, "profile showのtool契約versionを読めません")
            let raw = String(data: fixture("launcher-profile-show.json"), encoding: .utf8) ?? ""
            check(!raw.contains("sk-or-"), "profile showに鍵らしき文字列があります")
        }

        if let catalog = decode(ProfileBridge.Catalog.self, "launcher-models-list.json") {
            let byId = Dictionary(uniqueKeysWithValues: catalog.models.map { ($0.id, $0) })
            check(
                byId["deepseek/deepseek-v4-flash-0731"]?.toolSupport == "verified",
                "検証済み状態を読めません"
            )
            check(
                byId["deepseek/deepseek-v4-flash-0731"]?.toolProviderAttempt == 2,
                "検証providerの試行番号を読めません"
            )
            check(
                byId["vendor/chat-only"]?.toolSupport == "unsupported",
                "非対応状態を読めません"
            )
        }

        // optional fieldが無い旧CLI出力も画面全体を落とさず読める。
        if let snapshot = decode(
            ProfileBridge.Snapshot.self, "launcher-profile-show-legacy.json"
        ) {
            check(snapshot.available.first?.toolSupport == nil, "旧profileにtool状態が生えています")
        }
        if let catalog = decode(
            ProfileBridge.Catalog.self, "launcher-models-list-legacy.json"
        ) {
            check(catalog.models.first?.toolSupport == nil, "旧候補にtool状態が生えています")
        }

        if failures.isEmpty {
            print("DECODER COMPAT: PASS")
            exit(0)
        }
        FileHandle.standardError.write(Data("DECODER COMPAT: FAIL\n".utf8))
        for failure in failures {
            FileHandle.standardError.write(Data("- \(failure)\n".utf8))
        }
        exit(1)
    }
}
