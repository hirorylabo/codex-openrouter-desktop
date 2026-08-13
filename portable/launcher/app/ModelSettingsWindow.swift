import AppKit

/// pickerへ出すモデルを選ぶ画面。
///
/// 任意slugの入力口は無い。OpenRouterが実際に配信しているモデルの中から選ぶ。
/// 検証も保存もPython CLIが行い、ここは入力の整合（最低1件・既定は選択内）を
/// 先に潰して無駄な往復を避けるだけ。
///
/// 画面は2段階で埋まる。`profile show` は即返るので現在の選択をすぐ描き、
/// 候補一覧（`models list`、ネットワークを触りうる）は後から流し込む。
final class ModelSettingsWindow: NSObject, NSWindowDelegate {
    let window: NSWindow
    private let table = ModelCatalogTable()
    private let searchField = NSSearchField()
    private let sortPopUp = NSPopUpButton()
    private let zdrOnly = NSButton(checkboxWithTitle: "ZDRのみ", target: nil, action: nil)
    private let noTrainingOnly = NSButton(checkboxWithTitle: "学習なしのみ", target: nil, action: nil)
    private let freeOnly = NSButton(checkboxWithTitle: "無料のみ", target: nil, action: nil)
    private let reasoningOnly = NSButton(checkboxWithTitle: "reasoningのみ", target: nil, action: nil)
    private let countLabel = NSTextField(labelWithString: "")
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
    private var saving = false
    private var catalogLoaded = false
    private var catalogNotice = ""

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
            contentRect: NSRect(x: 0, y: 0, width: 860, height: 620),
            styleMask: [.titled, .closable, .resizable],
            backing: .buffered,
            defer: false
        )
        super.init()

        guardrailButton.onPress = { [weak self] in self?.openGuardrail() }
        saveButton.onPress = { [weak self] in self?.save() }
        window.title = "モデル設定"
        window.delegate = self
        window.isReleasedWhenClosed = false
        window.setContentSize(NSSize(width: 860, height: 620))
        saveButton.keyEquivalent = "\r"
        saveButton.isEnabled = false
        guardrailButton.isEnabled = false

        let heading = NSTextField(
            labelWithString: "純正pickerへ出すモデルを選びます。価格はOpenRouter公表の参考値です。"
        )
        heading.textColor = .secondaryLabelColor

        table.onToggle = { [weak self] model, wanted in self?.toggle(model, wanted: wanted) }
        table.scrollView.heightAnchor.constraint(greaterThanOrEqualToConstant: 280).isActive = true

        window.contentView = buildLayout(heading: heading)
        window.center()
        statusLabel.stringValue = "読み込んでいます…"
        progress.startAnimation(nil)
    }

    private func buildLayout(heading: NSTextField) -> NSView {
        searchField.placeholderString = "モデル名で絞り込む"
        searchField.target = self
        searchField.action = #selector(filtersChanged)
        searchField.sendsSearchStringImmediately = false

        for checkbox in [zdrOnly, noTrainingOnly, freeOnly, reasoningOnly] {
            checkbox.target = self
            checkbox.action = #selector(filtersChanged)
        }
        zdrOnly.state = .on
        zdrOnly.toolTip = "ZDR（Zero Data Retention）で動くモデルだけを表示します。"
        noTrainingOnly.toolTip = "学習しないと確認できたモデルだけを表示します。"

        sortPopUp.target = self
        sortPopUp.action = #selector(sortChanged)
        for (title, tag) in [
            ("公開日が新しい順", ModelCatalogTable.Sort.released),
            ("7dトークン利用量が多い順", .usage),
            ("入力価格が安い順", .inputPrice),
            ("出力価格が安い順", .outputPrice),
            ("名前順", .name),
        ] {
            sortPopUp.addItem(withTitle: title)
            sortPopUp.lastItem?.tag = tag.rawValue
        }

        let filterRow = NSStackView(views: [
            searchField, zdrOnly, noTrainingOnly, freeOnly, reasoningOnly,
        ])
        filterRow.orientation = .horizontal
        filterRow.spacing = 10
        searchField.widthAnchor.constraint(greaterThanOrEqualToConstant: 200).isActive = true

        countLabel.textColor = .secondaryLabelColor
        countLabel.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        let countSpacer = NSView()
        countSpacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let sortRow = NSStackView(views: [
            NSTextField(labelWithString: "並び順:"), sortPopUp, countSpacer, countLabel,
        ])
        sortRow.orientation = .horizontal
        sortRow.spacing = 8

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
        statusLabel.maximumNumberOfLines = 3
        statusLabel.lineBreakMode = .byWordWrapping
        statusLabel.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        let statusRow = NSStackView(views: [progress, statusLabel])
        statusRow.orientation = .horizontal
        statusRow.spacing = 8

        let spacer = NSView()
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let buttons = NSStackView(views: [guardrailButton, spacer, saveButton])
        buttons.orientation = .horizontal
        buttons.spacing = 12

        let root = NSStackView(views: [
            heading, filterRow, sortRow, table.scrollView, defaultRow, statusRow, buttons,
        ])
        root.orientation = .vertical
        root.alignment = .width
        root.spacing = 12
        root.edgeInsets = NSEdgeInsets(top: 20, left: 24, bottom: 20, right: 24)
        return root
    }

    /// 既に読み込み済みなら読み直さない。⌘,で前面へ戻すたびに読み直すと、
    /// 途中まで付け外した選択が黙って巻き戻る。
    func present() {
        window.makeKeyAndOrderFront(nil)
        if snapshot == nil && !saving {
            reload()
        }
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
            guardrailButton.isEnabled = URL(string: snapshot.guardrailUrl) != nil
            showInstalledOnly(snapshot)
            refreshControls()
            loadCatalog(refresh: false)
        }
    }

    /// 候補一覧が届くまでは、導入済みregistryの分だけで表を埋める。
    /// ネットワークが遅い間も、いま何が選ばれているかは見えていてほしい。
    private func showInstalledOnly(_ snapshot: ProfileBridge.Snapshot) {
        let placeholders = snapshot.available.map { option in
            ProfileBridge.CatalogEntry(
                id: option.id,
                displayName: option.displayName,
                description: option.capability,
                created: nil,
                contextWindow: option.contextWindow,
                efforts: option.efforts,
                defaultEffort: option.defaultEffort,
                zdrSupported: option.zdrSupported ?? true,
                trainsOnData: (option.zdrSupported ?? true) ? false : nil,
                free: false,
                headline: ProfileBridge.CatalogEntry.Price(input: "—", output: "—", cacheRead: nil),
                usageTokens: nil
            )
        }
        table.update(
            entries: placeholders,
            selected: selected,
            editable: isEditable,
            usageAvailable: false
        )
    }

    private func loadCatalog(refresh: Bool) {
        progress.startAnimation(nil)
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let outcome = Result { try ProfileBridge.catalog(refresh: refresh) }
            DispatchQueue.main.async { self?.finishCatalog(outcome) }
        }
    }

    private func finishCatalog(_ outcome: Result<ProfileBridge.Catalog, Error>) {
        progress.stopAnimation(nil)
        switch outcome {
        case .failure(let error):
            // 候補が引けなくても、いま入っているモデルの編集は続けられる。
            catalogNotice = "候補一覧を取得できませんでした（導入済みのみ表示）。"
                + "\n\(error.localizedDescription)"
        case .success(let catalog):
            catalogLoaded = true
            catalogNotice = ""
            table.update(
                entries: catalog.models,
                selected: selected,
                editable: isEditable,
                usageAvailable: catalog.usageAvailable
            )
        }
        refreshControls()
    }

    // --- 入力 --------------------------------------------------------------
    @objc private func filtersChanged(_ sender: Any) {
        table.filters = ModelCatalogTable.Filters(
            zdrOnly: zdrOnly.state == .on,
            noTrainingOnly: noTrainingOnly.state == .on,
            freeOnly: freeOnly.state == .on,
            reasoningOnly: reasoningOnly.state == .on,
            search: searchField.stringValue
        )
        refreshControls()
    }

    @objc private func sortChanged(_ sender: NSPopUpButton) {
        guard let tag = sender.selectedItem?.tag,
              let sort = ModelCatalogTable.Sort(rawValue: tag) else { return }
        table.sort = sort
        refreshControls()
    }

    private func toggle(_ model: String, wanted: Bool) {
        guard isEditable else { return }
        if wanted {
            guard let entry = table.entry(id: model) else { return }
            if entry.zdrSupported {
                apply(selection: selected.union([model]))
            } else {
                confirmNonZdr(entry)
            }
            return
        }
        var next = selected
        next.remove(model)
        apply(selection: next)
    }

    /// ZDRなしのモデルは既定の安全性を下げる。黙って通さず、何が変わるかを出す。
    private func confirmNonZdr(_ entry: ProfileBridge.CatalogEntry) {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "\(entry.displayName) はZDRなしで動作します"
        alert.informativeText =
            "このモデルにはZero Data Retentionのendpointがありません。追加すると、"
            + "このモデルへ送った内容がproviderに保持される可能性があります。"
            + "\n\n他のモデルのZDR強制はそのままです。"
        alert.addButton(withTitle: "追加する")
        alert.addButton(withTitle: "やめる")
        alert.beginSheetModal(for: window) { [weak self] response in
            guard let self, response == .alertFirstButtonReturn else { return }
            self.apply(selection: self.selected.union([entry.id]))
        }
    }

    private func apply(selection: Set<String>) {
        // 既定モデルを外したら、新しい既定を明示選択するまで保存させない。
        // 黙って別モデルへ寄せると、次回起動で意図しないモデルが選ばれる。
        if let defaultModel, !selection.contains(defaultModel) {
            self.defaultModel = nil
        }
        selected = selection
        table.setSelected(selected, editable: isEditable)
        refreshControls()
    }

    @objc private func defaultChanged(_ sender: NSPopUpButton) {
        guard let item = sender.selectedItem, item.title != Self.placeholderTitle else { return }
        defaultModel = item.representedObject as? String
        refreshControls()
    }

    // --- 状態 --------------------------------------------------------------

    private var isEditable: Bool { (snapshot?.editable ?? false) && !saving }

    /// 保存できる既定モデル。選択集合の中に無ければ「未選択」として扱う。
    ///
    /// 保存可否・popupの選択・保存payloadがこの1箇所を見る。別々に判定すると、
    /// 保存ボタンだけが理由も出さずに無効、のような食い違いが生まれる。
    private var resolvedDefault: String? {
        guard let defaultModel, selected.contains(defaultModel) else { return nil }
        return defaultModel
    }

    /// 保存へ送る順序。並び順自体はCLIがregistry順へ正規化するので、ここは
    /// 「選択を1件も落とさない」ことだけを保証する。
    private func orderedSelection() -> [String] {
        var ordered = table.entries.map(\.id).filter(selected.contains)
        ordered += selected.subtracting(ordered).sorted()
        return ordered
    }

    private func title(of model: String) -> String {
        table.entry(id: model)?.displayName
            ?? snapshot?.available.first { $0.id == model }?.displayName
            ?? model
    }

    private func refreshControls() {
        guard snapshot != nil else { return }
        let editable = isEditable
        for checkbox in [zdrOnly, noTrainingOnly, freeOnly, reasoningOnly] {
            checkbox.isEnabled = catalogLoaded
        }
        searchField.isEnabled = catalogLoaded
        sortPopUp.isEnabled = catalogLoaded
        defaultPopUp.isEnabled = editable && !selected.isEmpty
        countLabel.stringValue = "表示 \(table.visibleCount)件 / 選択 \(selected.count)件"

        let usable = resolvedDefault
        defaultPopUp.removeAllItems()
        if usable == nil {
            defaultPopUp.addItem(withTitle: Self.placeholderTitle)
            defaultPopUp.item(at: 0)?.isEnabled = false
        }
        for model in orderedSelection() {
            defaultPopUp.addItem(withTitle: title(of: model))
            defaultPopUp.lastItem?.representedObject = model
            if model == usable {
                defaultPopUp.select(defaultPopUp.lastItem)
            }
        }

        saveButton.isEnabled = editable && !selected.isEmpty && usable != nil
        statusLabel.stringValue = guidance()
    }

    private func guidance() -> String {
        if saving {
            return "OpenRouterで呼び出せるか確認しています…"
        }
        if snapshot?.editable == false {
            return "ChatGPT終了後に変更できます。"
        }
        if selected.isEmpty {
            return "最低1モデルを選択してください。"
        }
        if resolvedDefault == nil {
            return "既定モデルを選び直してください。"
        }
        if !catalogNotice.isEmpty {
            return catalogNotice
        }
        let withoutZdr = selected.filter { table.entry(id: $0)?.zdrSupported == false }
        if !withoutZdr.isEmpty {
            return "ZDRなしのモデルを\(withoutZdr.count)件選んでいます。"
                + "そのモデルへ送った内容はproviderに保持される可能性があります。"
        }
        return ""
    }

    // --- 保存 --------------------------------------------------------------
    private func openGuardrail() {
        guard let snapshot, let url = URL(string: snapshot.guardrailUrl) else { return }
        NSWorkspace.shared.open(url)
    }

    private func save() {
        guard let defaultModel = resolvedDefault, !saving else { return }
        let models = orderedSelection()
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
                        self.table.setSelected(self.selected, editable: self.isEditable)
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
