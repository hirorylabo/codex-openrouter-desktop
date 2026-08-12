import AppKit
import Foundation

final class CodexOpenRouterLauncher: NSObject, NSApplicationDelegate {
    private let userHome = FileManager.default.homeDirectoryForCurrentUser.path
    private lazy var launcherPath = "\(userHome)/.local/bin/codex-openrouter-app"
    private lazy var defaultWorkspace =
        Bundle.main.object(forInfoDictionaryKey: "CodexDefaultWorkspace") as? String
        ?? "\(userHome)/Documents"
    // 案Dでは専用cloneを作らない。前面化の相手は純正appそのもの。
    private let stockAppURL = URL(fileURLWithPath: "/Applications/ChatGPT.app").standardizedFileURL
    // 出所は UserPaths.state_dir。build_launcher がInfo.plistへ書く。
    private lazy var launcherLogPath =
        Bundle.main.object(forInfoDictionaryKey: "CodexLauncherLog") as? String
        ?? "\(userHome)/.local/share/codex-openrouter-desktop/state/logs/launcher.log"
    private var receivedWorkspace = false
    private var launchInProgress = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) { [weak self] in
            guard let self else { return }
            if !self.receivedWorkspace {
                self.launch(workspace: self.defaultWorkspace)
            }
        }
    }

    func application(_ application: NSApplication, open urls: [URL]) {
        guard let workspace = urls.first?.path else { return }
        receivedWorkspace = true
        launch(workspace: workspace)
    }

    func application(_ sender: NSApplication, openFiles filenames: [String]) {
        guard let workspace = filenames.first else {
            sender.reply(toOpenOrPrint: .failure)
            return
        }
        receivedWorkspace = true
        launch(workspace: workspace)
        sender.reply(toOpenOrPrint: .success)
    }

    private func launch(workspace: String) {
        guard !launchInProgress else { return }
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: workspace, isDirectory: &isDirectory),
              isDirectory.boolValue else {
            showError("workspaceはフォルダーを指定してください:\n\(workspace)")
            NSApplication.shared.terminate(nil)
            return
        }

        launchInProgress = true
        let process = Process()
        let outputPipe = Pipe()
        process.executableURL = URL(fileURLWithPath: launcherPath)
        process.arguments = [workspace]
        process.standardOutput = outputPipe
        process.standardError = outputPipe
        do {
            try process.run()
        } catch {
            showError("Codex OpenRouterを起動できませんでした。\n\(error.localizedDescription)")
            NSApplication.shared.terminate(nil)
            return
        }

        // supervisorは self-heal → catalog再生成 → guard起動 を済ませてから純正appを
        // 起動する。Codex更新直後は catalog再生成の分だけ待たされるので、出てくるまで
        // ポーリングする。プロセスが先に終われば打ち切る。
        pollForStockWindow(process: process, deadline: Date().addingTimeInterval(60))

        DispatchQueue.global(qos: .userInitiated).async {
            process.waitUntilExit()
            let data = outputPipe.fileHandleForReading.readDataToEndOfFile()
            let output = String(data: data, encoding: .utf8) ?? ""
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                if process.terminationStatus != EXIT_SUCCESS {
                    self.showError(
                        "起動または検証に失敗しました。\n\n" +
                        String(output.suffix(4000)) + "\n\n詳細: \(self.launcherLogPath)"
                    )
                }
                NSApplication.shared.terminate(nil)
            }
        }
    }

    /// 純正appが現れるまで待って一度だけ前面へ出す。
    private func pollForStockWindow(process: Process, deadline: Date) {
        guard process.isRunning else { return }
        if activateStockWindow() { return }
        guard Date() < deadline else { return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
            self?.pollForStockWindow(process: process, deadline: deadline)
        }
    }

    /// supervisorはbundle内のexecutableを直接起動するが、bundleURLは .app を指す。
    @discardableResult
    private func activateStockWindow() -> Bool {
        guard let application = NSWorkspace.shared.runningApplications.first(where: {
            $0.bundleURL?.standardizedFileURL == stockAppURL
        }) else {
            return false
        }
        application.activate(options: [.activateAllWindows])
        return true
    }

    private func showError(_ message: String) {
        let alert = NSAlert()
        alert.alertStyle = .critical
        alert.messageText = "Codex OpenRouter"
        alert.informativeText = message
        alert.runModal()
    }
}

let application = NSApplication.shared
let launcherDelegate = CodexOpenRouterLauncher()
application.delegate = launcherDelegate
application.run()
