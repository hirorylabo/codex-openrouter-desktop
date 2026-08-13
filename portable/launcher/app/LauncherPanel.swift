import AppKit

/// 起動時とfolder drop時に出る管理画面。
///
/// ここではChatGPTを起動しない。何が起きるかを見せてから、利用者が押した時だけ
/// 起動する。folder dropでworkspaceだけ差し替わるのも同じ理由。
final class LauncherPanel: NSObject, NSWindowDelegate {
    let window: NSWindow
    private let summaryLabel = NSTextField(labelWithString: "モデル構成を読み込んでいます…")
    private let defaultLabel = NSTextField(labelWithString: "")
    private let workspaceLabel = NSTextField(labelWithString: "")
    private let noticeLabel = NSTextField(labelWithString: "")
    private let settingsButton: ActionButton
    private let launchButton: ActionButton
    private let onClose: () -> Void

    init(
        onLaunch: @escaping () -> Void,
        onSettings: @escaping () -> Void,
        onClose: @escaping () -> Void
    ) {
        self.onClose = onClose
        settingsButton = ActionButton(title: "モデル設定…")
        launchButton = ActionButton(title: "OpenRouterで起動")
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 460, height: 190),
            styleMask: [.titled, .closable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        super.init()

        settingsButton.onPress = onSettings
        launchButton.onPress = onLaunch
        window.title = "Codex OpenRouter"
        window.delegate = self
        window.isReleasedWhenClosed = false
        launchButton.keyEquivalent = "\r"
        settingsButton.isEnabled = false

        for label in [defaultLabel, workspaceLabel, noticeLabel] {
            label.lineBreakMode = .byTruncatingMiddle
            label.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        }
        summaryLabel.font = .systemFont(ofSize: NSFont.systemFontSize + 2, weight: .semibold)
        workspaceLabel.textColor = .secondaryLabelColor
        noticeLabel.textColor = .secondaryLabelColor
        noticeLabel.maximumNumberOfLines = 2

        let spacer = NSView()
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let buttons = NSStackView(views: [settingsButton, spacer, launchButton])
        buttons.orientation = .horizontal
        buttons.spacing = 12

        let root = NSStackView(views: [
            summaryLabel, defaultLabel, workspaceLabel, noticeLabel, buttons,
        ])
        root.orientation = .vertical
        root.alignment = .width
        root.spacing = 8
        root.setCustomSpacing(18, after: noticeLabel)
        root.edgeInsets = NSEdgeInsets(top: 20, left: 24, bottom: 20, right: 24)
        window.contentView = root
        window.center()
    }

    func present() {
        window.makeKeyAndOrderFront(nil)
    }

    func hide() {
        window.orderOut(nil)
    }

    func show(workspace: String) {
        workspaceLabel.stringValue = "workspace: \(workspace)"
    }

    func show(snapshot: ProfileBridge.Snapshot) {
        let names = Dictionary(
            uniqueKeysWithValues: snapshot.available.map { ($0.id, $0.displayName) }
        )
        let count = snapshot.profile.models.count
        summaryLabel.stringValue = "表示モデル \(count)件"
        let defaultModel = snapshot.profile.defaultModel
        defaultLabel.stringValue = "既定モデル: \(names[defaultModel] ?? defaultModel)"
        noticeLabel.stringValue = snapshot.editable
            ? ""
            : "OpenRouterモードが実行中です。ChatGPT終了後に変更できます。"
        settingsButton.isEnabled = true
    }

    func show(failure: String) {
        summaryLabel.stringValue = "モデル構成を読み込めません"
        defaultLabel.stringValue = ""
        noticeLabel.stringValue = failure
        settingsButton.isEnabled = false
    }

    func setLaunching(_ launching: Bool) {
        launchButton.isEnabled = !launching
        settingsButton.isEnabled = !launching
    }

    func windowWillClose(_ notification: Notification) {
        onClose()
    }
}

/// targetを持たないボタンにclosureを結び付けるための最小のadapter。
///
/// `onPress` は生成後に差し込む。所有者の `super.init()` より前にselfへ触れないため。
/// 名前を `perform` にしないのは `NSObject.perform(_:)` と衝突するから。
final class ActionButton: NSButton {
    var onPress: () -> Void = {}

    init(title: String) {
        super.init(frame: .zero)
        self.title = title
        bezelStyle = .rounded
        target = self
        action = #selector(fire)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("nibからは生成しない")
    }

    @objc private func fire() {
        onPress()
    }
}
