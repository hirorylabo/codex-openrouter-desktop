import AppKit
import Foundation

/// `Codex OpenRouter.app` の本体。小型の管理ランチャーとして振る舞う。
///
/// 常駐daemonにはしない。管理画面を閉じるか、OpenRouterセッションが終われば
/// ランチャーも終わる。純正 `/Applications/ChatGPT.app` は一切変更しない。
final class LauncherApp: NSObject, NSApplicationDelegate, NSMenuItemValidation {
    private let userHome = FileManager.default.homeDirectoryForCurrentUser.path
    private lazy var helperPath = "\(userHome)/.local/bin/codex-openrouter-app"
    private lazy var fallbackWorkspace =
        Bundle.main.object(forInfoDictionaryKey: "CodexDefaultWorkspace") as? String
        ?? "\(userHome)/Documents"
    // 案Dでは専用cloneを作らない。前面化の相手は純正appそのもの。
    private let stockAppURL = URL(fileURLWithPath: "/Applications/ChatGPT.app").standardizedFileURL
    // 出所は UserPaths.state_dir。build_launcher がInfo.plistへ書く。
    private lazy var launcherLogPath =
        Bundle.main.object(forInfoDictionaryKey: "CodexLauncherLog") as? String
        ?? "\(userHome)/.local/share/codex-openrouter-desktop/state/logs/launcher.log"
    // upgrade.py の STATUS_UPDATING / STATUS_LAUNCHING と同じ文字列であること。
    private static let statusUpdating = "STATUS: updating"
    private static let statusLaunching = "STATUS: launching"

    private var workspace = ""
    private var panel: LauncherPanel?
    private var settingsWindow: ModelSettingsWindow?
    private var launchInProgress = false
    private var outputTail = ""
    private var progressWindow: NSWindow?
    private var ignoredStockProcessIdentifiers = Set<pid_t>()

    // MARK: - ライフサイクル

    func applicationWillFinishLaunching(_ notification: Notification) {
        installMainMenu()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        if workspace.isEmpty {
            workspace = fallbackWorkspace
        }
        presentPanel()
        NSApplication.shared.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        // 起動中は管理画面を閉じたまま生き残る必要がある。終了判断は自分で持つ。
        false
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        if launchInProgress {
            activateStockWindow()
        } else {
            presentPanel()
        }
        return true
    }

    /// folder dropとOpen With。workspaceだけ差し替え、起動は利用者の操作を待つ。
    func application(_ application: NSApplication, open urls: [URL]) {
        guard let path = urls.first?.path else { return }
        adopt(workspace: path)
    }

    func application(_ sender: NSApplication, openFiles filenames: [String]) {
        guard let path = filenames.first, adopt(workspace: path) else {
            sender.reply(toOpenOrPrint: .failure)
            return
        }
        sender.reply(toOpenOrPrint: .success)
    }

    @discardableResult
    private func adopt(workspace path: String) -> Bool {
        // 起動後のdropは受け取らない。workspaceは起動時に純正appへ渡り済みで、
        // ここで差し替えても効かないうえ、隠した管理画面が戻ってくる。
        guard !launchInProgress else {
            activateStockWindow()
            return false
        }
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: path, isDirectory: &isDirectory),
              isDirectory.boolValue else {
            showError("workspaceはフォルダーを指定してください:\n\(path)")
            return false
        }
        workspace = path
        presentPanel()
        return true
    }

    // MARK: - メニュー

    private func installMainMenu() {
        let name = "Codex OpenRouter"
        let mainMenu = NSMenu()

        let applicationItem = NSMenuItem()
        let applicationMenu = NSMenu()
        applicationMenu.addItem(
            withTitle: "\(name)について",
            action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)),
            keyEquivalent: ""
        )
        applicationMenu.addItem(.separator())
        // macOS標準の設定入口。⌘, とAppメニューの両方から開く。
        applicationMenu.addItem(
            withTitle: "設定…", action: #selector(openSettings(_:)), keyEquivalent: ","
        )
        applicationMenu.addItem(.separator())
        applicationMenu.addItem(
            withTitle: "\(name)を隠す", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h"
        )
        let hideOthers = applicationMenu.addItem(
            withTitle: "ほかを隠す",
            action: #selector(NSApplication.hideOtherApplications(_:)),
            keyEquivalent: "h"
        )
        hideOthers.keyEquivalentModifierMask = [.command, .option]
        applicationMenu.addItem(
            withTitle: "すべてを表示",
            action: #selector(NSApplication.unhideAllApplications(_:)),
            keyEquivalent: ""
        )
        applicationMenu.addItem(.separator())
        applicationMenu.addItem(
            withTitle: "\(name)を終了",
            action: #selector(NSApplication.terminate(_:)),
            keyEquivalent: "q"
        )
        applicationItem.submenu = applicationMenu
        mainMenu.addItem(applicationItem)

        let windowItem = NSMenuItem()
        let windowMenu = NSMenu(title: "ウインドウ")
        windowMenu.addItem(
            withTitle: "しまう", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m"
        )
        windowMenu.addItem(
            withTitle: "閉じる", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w"
        )
        windowItem.submenu = windowMenu
        mainMenu.addItem(windowItem)

        NSApplication.shared.mainMenu = mainMenu
        NSApplication.shared.windowsMenu = windowMenu
    }

    func validateMenuItem(_ menuItem: NSMenuItem) -> Bool {
        if menuItem.action == #selector(openSettings(_:)) {
            return !launchInProgress
        }
        return true
    }

    // MARK: - 管理画面

    private func presentPanel() {
        if panel == nil {
            panel = LauncherPanel(
                onLaunch: { [weak self] in self?.startOpenRouter() },
                onSettings: { [weak self] in self?.openSettings(nil) },
                onClose: { [weak self] in self?.panelClosed() }
            )
        }
        panel?.show(workspace: workspace)
        panel?.present()
        refreshPanel()
    }

    private func refreshPanel() {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let outcome = Result { try ProfileBridge.show() }
            DispatchQueue.main.async {
                guard let panel = self?.panel else { return }
                switch outcome {
                case .success(let snapshot):
                    panel.show(snapshot: snapshot)
                case .failure(let error):
                    panel.show(failure: error.localizedDescription)
                }
            }
        }
    }

    private func panelClosed() {
        // 管理画面を閉じたら終わる。起動済みなら supervisor の終了で終わる。
        guard !launchInProgress else { return }
        settingsWindow?.window.close()
        DispatchQueue.main.async { NSApplication.shared.terminate(nil) }
    }

    @objc private func openSettings(_ sender: Any?) {
        guard !launchInProgress else { return }
        if settingsWindow == nil {
            settingsWindow = ModelSettingsWindow(
                onClose: { [weak self] in self?.settingsWindow = nil },
                onApplied: { [weak self] snapshot in self?.panel?.show(snapshot: snapshot) }
            )
        }
        settingsWindow?.present()
    }

    // MARK: - 起動

    private func startOpenRouter() {
        guard !launchInProgress else { return }
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: workspace, isDirectory: &isDirectory),
              isDirectory.boolValue else {
            showError("workspaceはフォルダーを指定してください:\n\(workspace)")
            return
        }

        launchInProgress = true
        panel?.setLaunching(true)
        settingsWindow?.window.close()

        let stockApplications = runningStockApplications()
        guard !stockApplications.isEmpty else {
            beginHelper()
            return
        }

        guard confirmSwitchFromStockApp() else {
            launchInProgress = false
            panel?.setLaunching(false)
            stockApplications.first?.activate(options: [.activateAllWindows])
            return
        }

        ignoredStockProcessIdentifiers = Set(stockApplications.map(\.processIdentifier))
        var terminationRequested = true
        for application in stockApplications {
            if !application.terminate() {
                terminationRequested = false
            }
        }
        guard terminationRequested else {
            launchInProgress = false
            panel?.setLaunching(false)
            showError("純正ChatGPTへ終了を要求できませんでした。\n手動で終了してから、もう一度お試しください。")
            return
        }

        showProgress("純正ChatGPTの終了を待っています…")
        waitForStockTermination(
            processIdentifiers: ignoredStockProcessIdentifiers,
            deadline: Date().addingTimeInterval(30)
        ) { [weak self] terminated in
            guard let self else { return }
            self.hideProgress()
            guard terminated else {
                self.launchInProgress = false
                self.panel?.setLaunching(false)
                self.showError(
                    "純正ChatGPTが30秒以内に終了しませんでした。\n" +
                    "手動で終了してから、もう一度お試しください。"
                )
                return
            }
            self.beginHelper()
        }
    }

    private func beginHelper() {
        panel?.hide()
        showProgress("OpenRouterモードを準備しています…")
        startLauncherHelper(workspace: workspace)
    }

    private func startLauncherHelper(workspace: String) {
        let process = Process()
        let outputPipe = Pipe()
        process.executableURL = URL(fileURLWithPath: helperPath)
        process.arguments = [workspace]
        process.standardOutput = outputPipe
        process.standardError = outputPipe
        do {
            try process.run()
        } catch {
            hideProgress()
            showError("Codex OpenRouterを起動できませんでした。\n\(error.localizedDescription)")
            NSApplication.shared.terminate(nil)
            return
        }

        // 出力は逐次読む。終了後にまとめて読むと、出力がpipe bufferを超えたときに
        // 子が書き込みでブロックして進まなくなる。
        outputPipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            DispatchQueue.main.async { self?.consume(text) }
        }

        // supervisorは self-heal → catalog再生成 → guard起動 を済ませてから純正appを
        // 起動する。Codex更新直後や自動更新が走ったときはその分待たされるので、
        // 出てくるまでポーリングする。プロセスが先に終われば打ち切る。
        pollForStockWindow(process: process, deadline: Date().addingTimeInterval(180))

        DispatchQueue.global(qos: .userInitiated).async {
            process.waitUntilExit()
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                outputPipe.fileHandleForReading.readabilityHandler = nil
                self.hideProgress()
                if process.terminationStatus != EXIT_SUCCESS {
                    self.showError(
                        "起動または検証に失敗しました。\n\n" +
                        String(self.outputTail.suffix(4000)) + "\n\n詳細: \(self.launcherLogPath)"
                    )
                }
                NSApplication.shared.terminate(nil)
            }
        }
    }

    private func runningStockApplications() -> [NSRunningApplication] {
        NSWorkspace.shared.runningApplications.filter {
            !$0.isTerminated && $0.bundleURL?.standardizedFileURL == stockAppURL
        }
    }

    private func confirmSwitchFromStockApp() -> Bool {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "純正ChatGPTを終了して切り替えますか？"
        alert.informativeText =
            "純正ChatGPTを通常終了し、OpenRouterモードで再起動します。" +
            "未保存の入力がある場合はキャンセルしてください。"
        alert.addButton(withTitle: "終了してOpenRouterモードへ切り替える")
        alert.addButton(withTitle: "キャンセル")
        NSApplication.shared.activate(ignoringOtherApps: true)
        return alert.runModal() == .alertFirstButtonReturn
    }

    private func waitForStockTermination(
        processIdentifiers: Set<pid_t>,
        deadline: Date,
        completion: @escaping (Bool) -> Void
    ) {
        let stillRunning = runningStockApplications().contains {
            processIdentifiers.contains($0.processIdentifier)
        }
        guard stillRunning else {
            completion(true)
            return
        }
        guard Date() < deadline else {
            completion(false)
            return
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { [weak self] in
            self?.waitForStockTermination(
                processIdentifiers: processIdentifiers,
                deadline: deadline,
                completion: completion
            )
        }
    }

    /// 子の出力を溜めつつ、進行状況のsentinelに反応する。
    private func consume(_ text: String) {
        outputTail += text
        if outputTail.count > 8000 {
            outputTail = String(outputTail.suffix(8000))
        }
        if text.contains(Self.statusUpdating) {
            showProgress("更新を適用しています…")
        }
        if text.contains(Self.statusLaunching) {
            hideProgress()
        }
    }

    /// 純正appが現れるまで待って一度だけ前面へ出す。
    private func pollForStockWindow(process: Process, deadline: Date) {
        guard process.isRunning else { return }
        if activateStockWindow() {
            hideProgress()
            return
        }
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
                && !ignoredStockProcessIdentifiers.contains($0.processIdentifier)
        }) else {
            return false
        }
        application.activate(options: [.activateAllWindows])
        return true
    }

    /// 自動更新は十数秒かかる。無表示だと起動しなかったように見えるのでHUDを出す。
    private func showProgress(_ message: String) {
        guard progressWindow == nil else { return }
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 320, height: 88),
            styleMask: [.titled, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        window.title = "Codex OpenRouter"
        window.titlebarAppearsTransparent = true
        window.isMovableByWindowBackground = true
        window.level = .floating
        window.center()

        let spinner = NSProgressIndicator()
        spinner.style = .spinning
        spinner.controlSize = .small
        spinner.startAnimation(nil)
        let label = NSTextField(labelWithString: message)
        let stack = NSStackView(views: [spinner, label])
        stack.orientation = .horizontal
        stack.spacing = 10
        stack.edgeInsets = NSEdgeInsets(top: 16, left: 20, bottom: 20, right: 20)
        window.contentView = stack

        window.makeKeyAndOrderFront(nil)
        NSApplication.shared.activate(ignoringOtherApps: true)
        progressWindow = window
    }

    private func hideProgress() {
        progressWindow?.orderOut(nil)
        progressWindow = nil
    }

    private func showError(_ message: String) {
        let alert = NSAlert()
        alert.alertStyle = .critical
        alert.messageText = "Codex OpenRouter"
        alert.informativeText = message
        alert.runModal()
    }
}
