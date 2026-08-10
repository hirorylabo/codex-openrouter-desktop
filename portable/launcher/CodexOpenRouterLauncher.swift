import AppKit
import Foundation

final class CodexOpenRouterLauncher: NSObject, NSApplicationDelegate {
    private let userHome = FileManager.default.homeDirectoryForCurrentUser.path
    private lazy var launcherPath = "\(userHome)/.local/bin/codex-openrouter-app"
    private lazy var defaultWorkspace =
        Bundle.main.object(forInfoDictionaryKey: "CodexDefaultWorkspace") as? String
        ?? "\(userHome)/Documents"
    private lazy var cloneExecutablePath =
        "\(userHome)/Applications/ChatGPT OpenRouter.app/Contents/MacOS/ChatGPT"
    private lazy var launcherLogPath = "\(userHome)/.codex-openrouter/logs/launcher.log"
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
                } else {
                    self.activateOpenRouterWindow()
                }
                NSApplication.shared.terminate(nil)
            }
        }
    }

    private func activateOpenRouterWindow() {
        let application = NSWorkspace.shared.runningApplications.first {
            $0.executableURL?.path == cloneExecutablePath
        }
        application?.activate(options: [.activateAllWindows])
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
