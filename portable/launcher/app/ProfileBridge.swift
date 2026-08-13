import Foundation

/// `codex-openrouter profile show|apply` の呼び出し口。
///
/// profile・Keychain・Guardrailの判断はPython CLIだけが持つ。Swift側へ同じ検証を
/// 書くと、片方だけ直された時に「UIでは通ったのに実体は違う」状態が生まれる。
/// ここは引数を組み立ててJSONを復号するだけに留める。
enum ProfileBridge {
    static let documentSchemaVersion = 1

    struct Failure: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }

    struct ModelOption: Decodable {
        let id: String
        let displayName: String
        let capability: String
        let efforts: [String]
        let defaultEffort: String?
        let contextWindow: Int?
        /// 省略時は安全側のtrue。非必須で読むのは、古いCLIと新しいランチャーが
        /// 一瞬でも噛み合ったときに、画面ごと「応答を解釈できません」で潰さないため。
        let zdrSupported: Bool?
    }

    struct Selection: Decodable {
        let name: String
        let models: [String]
        let defaultModel: String
    }

    struct Snapshot: Decodable {
        let schemaVersion: Int
        let profile: Selection
        let available: [ModelOption]
        let openrouterActive: Bool
        let editable: Bool
        let workspace: String
        let guardrailUrl: String
    }

    struct Outcome: Decodable {
        let schemaVersion: Int
        let result: String
        let profile: Selection
    }

    /// `models list --json` の1行。候補一覧の描画に必要な事実だけを持つ。
    struct CatalogEntry: Decodable {
        struct Price: Decodable {
            let input: String
            let output: String
            let cacheRead: String?
        }

        let id: String
        let displayName: String
        let description: String
        let created: Double?
        let contextWindow: Int?
        let efforts: [String]
        let defaultEffort: String?
        let zdrSupported: Bool
        /// nilは「不明」。学習ポリシーはprovider単位でしか公開されておらず、
        /// ZDR endpointを持たないmodelについては断定できない。
        let trainsOnData: Bool?
        let free: Bool
        let headline: Price
        let usageTokens: [String: String]?
    }

    struct Catalog: Decodable {
        let schemaVersion: Int
        let models: [CatalogEntry]
        let usageAvailable: Bool
    }

    static var executable: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".local/bin/codex-openrouter")
    }

    static func show() throws -> Snapshot {
        let snapshot: Snapshot = try decode(run(["profile", "show", "--json"], input: nil))
        try assertSupported(snapshot.schemaVersion)
        return snapshot
    }

    /// 候補一覧。`show` と違いネットワークを触りうるので、UIは別途非同期で呼ぶ。
    static func catalog(refresh: Bool = false) throws -> Catalog {
        var arguments = ["models", "list", "--json"]
        if refresh {
            arguments.append("--refresh")
        }
        let catalog: Catalog = try decode(run(arguments, input: nil))
        try assertSupported(catalog.schemaVersion)
        return catalog
    }

    static func apply(models: [String], defaultModel: String) throws -> Outcome {
        let payload: [String: Any] = [
            "schema_version": documentSchemaVersion,
            "models": models,
            "default_model": defaultModel,
        ]
        guard let body = try? JSONSerialization.data(withJSONObject: payload) else {
            throw Failure(message: "設定内容をJSONへ変換できませんでした。")
        }
        let outcome: Outcome = try decode(run(["profile", "apply", "--stdin-json"], input: body))
        try assertSupported(outcome.schemaVersion)
        return outcome
    }

    private static func assertSupported(_ version: Int) throws {
        guard version == documentSchemaVersion else {
            throw Failure(
                message: "codex-openrouterの応答schemaが新しすぎます（\(version)）。\n"
                    + "codex-openrouter upgrade でランチャーを更新してください。"
            )
        }
    }

    /// 標準出力だけをpipeで受け、標準エラーは一時ファイルへ逃がす。
    /// 両方をpipeにすると、片方を読み切るまでもう片方が64KBで詰まって停止しうる。
    private static func run(_ arguments: [String], input: Data?) throws -> Data {
        guard FileManager.default.isExecutableFile(atPath: executable.path) else {
            throw Failure(
                message: "codex-openrouterが見つかりません:\n\(executable.path)\n\n"
                    + "codex-openrouter setup を実行してください。"
            )
        }
        let errorLog = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("codex-openrouter-bridge-\(UUID().uuidString).log")
        guard FileManager.default.createFile(atPath: errorLog.path, contents: nil),
              let errorHandle = try? FileHandle(forWritingTo: errorLog) else {
            throw Failure(message: "作業ファイルを作成できませんでした。")
        }
        defer { try? FileManager.default.removeItem(at: errorLog) }

        let process = Process()
        let output = Pipe()
        let standardInput = Pipe()
        process.executableURL = executable
        process.arguments = arguments
        process.standardOutput = output
        process.standardError = errorHandle
        process.standardInput = standardInput
        do {
            try process.run()
        } catch {
            throw Failure(message: "codex-openrouterを起動できませんでした。\n\(error.localizedDescription)")
        }
        if let input {
            // 相手が先に死んでいる場合に例外で落とさない。終了コードと
            // 標準エラーの方が利用者へ見せる情報として正しい。
            try? standardInput.fileHandleForWriting.write(contentsOf: input)
        }
        try? standardInput.fileHandleForWriting.close()
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        try? errorHandle.close()

        guard process.terminationStatus == 0 else {
            let diagnostics = (try? String(contentsOf: errorLog, encoding: .utf8)) ?? ""
            let text = diagnostics.isEmpty ? String(data: data, encoding: .utf8) ?? "" : diagnostics
            let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
            throw Failure(
                message: trimmed.isEmpty
                    ? "codex-openrouterが終了コード \(process.terminationStatus) で失敗しました。"
                    : String(trimmed.suffix(2000))
            )
        }
        return data
    }

    /// 失敗時は受け取った本文の先頭も見せる。`profile show` / `apply` の出力に
    /// 秘密値は含まれないので、そのまま診断に使える。
    private static func decode<T: Decodable>(_ data: Data) throws -> T {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        do {
            return try decoder.decode(T.self, from: data)
        } catch {
            let received = String(data: data, encoding: .utf8)?
                .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            throw Failure(
                message: "codex-openrouterの応答を解釈できませんでした。\n\n"
                    + String(received.prefix(500))
            )
        }
    }
}
