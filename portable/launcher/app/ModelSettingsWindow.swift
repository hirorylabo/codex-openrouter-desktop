import AppKit

/// pickerへ出す検証済みモデルを選ぶ画面。
///
/// 任意slugの登録口は無い。同梱registryにあるモデルの出し入れと、そこからの
/// 既定モデル指定だけができる。検証も保存もPython CLIが行い、ここは入力の
/// 整合（最低1件・既定は選択内）を先に潰して無駄な往復を避けるだけ。
final class ModelSettingsWindow: NSObject, NSWindowDelegate {
    let window: NSWindow
    private let listStack = NSStackView()
    private let scrollView = NSScrollView()
    private let defaultPopUp = NSPopUpButton()
    private let statusLabel = NSTextField(labelWithString: "")
    private let progress = NSProgressIndicator()
    private let guardrailButton: ActionButton
    private let saveButton: ActionButton
    private let onClose: () -> Void
    private let onApplied: (ProfileBridge.Snapshot) -> Void

    private var snapshot: ProfileBridge.Snapshot?
    private var selected: Set<String> = []
    private var defaultModel: String?
    private var checkboxes: [NSButton] = []
    private var saving = false

    private static let placeholderTitle = "選択してください"

    init(
        onClose: @escaping () -> Void,
        onApplied: @escaping (ProfileBridge.Snapshot) -> Void
    ) {
        self.onClose = onClose
        self.onApplied = onApplied
        guardrailButton = ActionButton(title: "OpenRouter Guardrailを開く")
        saveButton = ActionButton(title: "検証して保存")
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 540, height: 520),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        super.init()

        guardrailButton.onPress = { [weak self] in self?.openGuardrail() }
        saveButton.onPress = { [weak self] in self?.save() }
        window.title = "モデル設定"
        window.delegate = self
        window.isReleasedWhenClosed = false
        saveButton.keyEquivalent = "\r"
        saveButton.isEnabled = false
        guardrailButton.isEnabled = false

        let heading = NSTextField(
            labelWithString: "純正pickerへ出す検証済みモデルを選びます。"
        )
        heading.textColor = .secondaryLabelColor

        listStack.orientation = .vertical
        listStack.alignment = .leading
        listStack.spacing = 12
        listStack.translatesAutoresizingMaskIntoConstraints = false
        scrollView.hasVerticalScroller = true
        scrollView.drawsBackground = false
        scrollView.borderType = .bezelBorder
        scrollView.documentView = listStack
        NSLayoutConstraint.activate([
            listStack.leadingAnchor.constraint(
                equalTo: scrollView.contentView.leadingAnchor, constant: 12
            ),
            listStack.trailingAnchor.constraint(
                equalTo: scrollView.contentView.trailingAnchor, constant: -12
            ),
            listStack.topAnchor.constraint(
                equalTo: scrollView.contentView.topAnchor, constant: 12
            ),
            scrollView.heightAnchor.constraint(greaterThanOrEqualToConstant: 300),
        ])

        defaultPopUp.target = self
        defaultPopUp.action = #selector(defaultChanged)
        defaultPopUp.isEnabled = false
        let defaultRow = NSStackView(views: [
            NSTextField(labelWithString: "既定モデル:"), defaultPopUp,
        ])
        defaultRow.orientation = .horizontal
        defaultRow.spacing = 8

        progress.style = .spinning
        progress.controlSize = .small
        progress.isDisplayedWhenStopped = false
        statusLabel.textColor = .secondaryLabelColor
        statusLabel.maximumNumberOfLines = 2
        statusLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        let statusRow = NSStackView(views: [progress, statusLabel])
        statusRow.orientation = .horizontal
        statusRow.spacing = 8

        let spacer = NSView()
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let buttons = NSStackView(views: [guardrailButton, spacer, saveButton])
        buttons.orientation = .horizontal
        buttons.spacing = 12

        let root = NSStackView(views: [heading, scrollView, defaultRow, statusRow, buttons])
        root.orientation = .vertical
        root.alignment = .width
        root.spacing = 12
        root.edgeInsets = NSEdgeInsets(top: 20, left: 24, bottom: 20, right: 24)
        window.contentView = root
        window.center()
        statusLabel.stringValue = "読み込んでいます…"
        progress.startAnimation(nil)
    }

    func present() {
        window.makeKeyAndOrderFront(nil)
        reload()
    }

    // --- 読み込み ----------------------------------------------------------
    private func reload() {
        progress.startAnimation(nil)
        statusLabel.stringValue = "読み込んでいます…"
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let outcome = Result { try ProfileBridge.show() }
            DispatchQueue.main.async { self?.finishReload(outcome) }
        }
    }

    private func finishReload(_ outcome: Result<ProfileBridge.Snapshot, Error>) {
        progress.stopAnimation(nil)
        switch outcome {
        case .failure(let error):
            statusLabel.stringValue = error.localizedDescription
            saveButton.isEnabled = false
        case .success(let snapshot):
            self.snapshot = snapshot
            selected = Set(snapshot.profile.models)
            defaultModel = snapshot.profile.defaultModel
            render(snapshot)
            refreshControls()
        }
    }

    private func render(_ snapshot: ProfileBridge.Snapshot) {
        for view in listStack.arrangedSubviews {
            listStack.removeArrangedSubview(view)
            view.removeFromSuperview()
        }
        checkboxes = []
        for (index, option) in snapshot.available.enumerated() {
            let checkbox = NSButton(
                checkboxWithTitle: option.displayName, target: self, action: #selector(modelToggled)
            )
            checkbox.tag = index
            checkbox.state = selected.contains(option.id) ? .on : .off
            checkboxes.append(checkbox)

            let detail = NSTextField(labelWithString: description(of: option))
            detail.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
            detail.textColor = .secondaryLabelColor
            detail.maximumNumberOfLines = 3
            detail.preferredMaxLayoutWidth = 440

            let row = NSStackView(views: [checkbox, detail])
            row.orientation = .vertical
            row.alignment = .leading
            row.spacing = 2
            row.edgeInsets = NSEdgeInsets(top: 0, left: 0, bottom: 0, right: 0)
            listStack.addArrangedSubview(row)
        }
        guardrailButton.isEnabled = URL(string: snapshot.guardrailUrl) != nil
    }

    private func description(of option: ProfileBridge.ModelOption) -> String {
        var text = "\(option.id) — \(option.capability)"
        if !option.efforts.isEmpty {
            let efforts = option.efforts.joined(separator: "/")
            let fallback = option.defaultEffort.map { "、既定 \($0)" } ?? ""
            text += "\nReasoning: \(efforts)\(fallback)"
        }
        return text
    }

    // --- 入力 --------------------------------------------------------------
    @objc private func modelToggled(_ sender: NSButton) {
        guard let snapshot, snapshot.available.indices.contains(sender.tag) else { return }
        let model = snapshot.available[sender.tag].id
        if sender.state == .on {
            selected.insert(model)
        } else {
            selected.remove(model)
            // 既定モデルを外したら、新しい既定を明示選択するまで保存させない。
            // 黙って別モデルへ寄せると、次回起動で意図しないモデルが選ばれる。
            if defaultModel == model {
                defaultModel = nil
            }
        }
        refreshControls()
    }

    @objc private func defaultChanged(_ sender: NSPopUpButton) {
        guard let item = sender.selectedItem, item.title != Self.placeholderTitle else { return }
        defaultModel = item.representedObject as? String
        refreshControls()
    }

    private func refreshControls() {
        guard let snapshot else { return }
        let editable = snapshot.editable && !saving
        for checkbox in checkboxes {
            checkbox.isEnabled = editable
        }
        defaultPopUp.isEnabled = editable && !selected.isEmpty

        defaultPopUp.removeAllItems()
        if defaultModel == nil {
            defaultPopUp.addItem(withTitle: Self.placeholderTitle)
            defaultPopUp.item(at: 0)?.isEnabled = false
        }
        for option in snapshot.available where selected.contains(option.id) {
            defaultPopUp.addItem(withTitle: option.displayName)
            defaultPopUp.lastItem?.representedObject = option.id
            if option.id == defaultModel {
                defaultPopUp.select(defaultPopUp.lastItem)
            }
        }

        let ready = !selected.isEmpty && defaultModel.map(selected.contains) == true
        saveButton.isEnabled = editable && ready
        statusLabel.stringValue = guidance(editable: snapshot.editable)
    }

    private func guidance(editable: Bool) -> String {
        if saving {
            return "OpenRouterのGuardrailと照合しています…"
        }
        if !editable {
            return "ChatGPT終了後に変更できます。"
        }
        if selected.isEmpty {
            return "最低1モデルを選択してください。"
        }
        if defaultModel == nil {
            return "既定モデルを選び直してください。"
        }
        return ""
    }

    // --- 保存 --------------------------------------------------------------
    private func openGuardrail() {
        guard let snapshot, let url = URL(string: snapshot.guardrailUrl) else { return }
        NSWorkspace.shared.open(url)
    }

    private func save() {
        guard let snapshot, let defaultModel, !saving else { return }
        let models = snapshot.available.map(\.id).filter(selected.contains)
        saving = true
        refreshControls()
        progress.startAnimation(nil)
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let outcome = Result {
                try ProfileBridge.apply(models: models, defaultModel: defaultModel)
            }
            DispatchQueue.main.async { self?.finishSave(outcome) }
        }
    }

    /// 保存後は必ず読み直してから編集を再開する。書けた内容とUIがずれたまま
    /// 次のsaveを受け付けると、利用者は自分が見ている集合を送ったつもりで
    /// 別の集合を送ることになる。
    private func finishSave(_ outcome: Result<ProfileBridge.Outcome, Error>) {
        switch outcome {
        case .failure(let error):
            saving = false
            progress.stopAnimation(nil)
            refreshControls()
            statusLabel.stringValue = ""
            let alert = NSAlert()
            alert.alertStyle = .warning
            alert.messageText = "保存できませんでした"
            alert.informativeText = error.localizedDescription
            alert.beginSheetModal(for: window)
        case .success(let result):
            let message = result.result == "unchanged"
                ? "変更はありません。"
                : "保存しました。次回のOpenRouter起動から反映されます。"
            DispatchQueue.global(qos: .userInitiated).async { [weak self] in
                let refreshed = try? ProfileBridge.show()
                DispatchQueue.main.async {
                    guard let self else { return }
                    self.saving = false
                    self.progress.stopAnimation(nil)
                    if let refreshed {
                        self.snapshot = refreshed
                        self.selected = Set(refreshed.profile.models)
                        self.defaultModel = refreshed.profile.defaultModel
                        self.render(refreshed)
                        self.onApplied(refreshed)
                    }
                    self.refreshControls()
                    self.statusLabel.stringValue = message
                }
            }
        }
    }

    func windowWillClose(_ notification: Notification) {
        onClose()
    }
}
