import AppKit

/// 候補一覧の並べ替え・絞り込み・行の描画。
///
/// 保存フロー（[ModelSettingsWindow.swift](ModelSettingsWindow.swift)）とは別の
/// 責務にしてある。候補は300件を超えるので、選択状態の管理と表の見せ方を同じ
/// ファイルへ混ぜると、どちらの都合で書かれた行なのか読めなくなる。
final class ModelCatalogTable: NSObject, NSTableViewDataSource, NSTableViewDelegate {
    private enum SortField: String {
        case model
        case input
        case output
        case released
        case usage
    }

    private struct Sort {
        var field: SortField
        var ascending: Bool
    }

    struct Filters {
        /// 既定でZDRのみ。安全側を既定にし、外すのは利用者の明示操作にする。
        var zdrOnly = true
        var noTrainingOnly = false
        var freeOnly = false
        var reasoningOnly = false
        var showUnsupported = false
        var search = ""
    }

    let tableView = NSTableView()
    let scrollView = NSScrollView()

    /// 選択が変わったときに呼ぶ。非ZDRを新たに選んだ場合は確認を挟みたいので、
    /// 「選ぼうとしている」段階で一度戻す。
    var onToggle: (String, Bool) -> Void = { _, _ in }

    private(set) var entries: [ProfileBridge.CatalogEntry] = []
    private var visible: [ProfileBridge.CatalogEntry] = []
    private var selected: Set<String> = []
    private var editable = false
    private var usageAvailable = false

    private var sort = Sort(field: .released, ascending: false)
    var filters = Filters() { didSet { refilter() } }

    private static let usageWindow = "7d"

    override init() {
        super.init()
        tableView.dataSource = self
        tableView.delegate = self
        tableView.usesAlternatingRowBackgroundColors = true
        tableView.allowsColumnSelection = false
        tableView.allowsMultipleSelection = false
        tableView.rowHeight = 42
        tableView.style = .inset

        addColumn("pick", title: "", width: 26)
        addColumn("model", title: "モデル", width: 225, sortField: .model)
        addColumn("input", title: "IN $/M", width: 74, sortField: .input)
        addColumn("output", title: "OUT $/M", width: 74, sortField: .output)
        addColumn("released", title: "公開日", width: 88, sortField: .released)
        addColumn("usage", title: "7dトークン", width: 88, sortField: .usage)
        addColumn("tool", title: "Codex tool / provider", width: 128)
        addColumn("badges", title: "", width: 100)
        tableView.sortDescriptors = [sortDescriptor(for: .released, ascending: false)]

        scrollView.documentView = tableView
        scrollView.hasVerticalScroller = true
        scrollView.borderType = .bezelBorder
        scrollView.translatesAutoresizingMaskIntoConstraints = false
    }

    private func addColumn(
        _ identifier: String,
        title: String,
        width: CGFloat,
        sortField: SortField? = nil
    ) {
        let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier(identifier))
        column.title = title
        column.width = width
        if let sortField {
            // 新しい列を最初に押したときは降順。もう一度押すとAppKitが昇順へ
            // 反転し、標準のsort indicatorもheaderへ表示する。
            column.sortDescriptorPrototype = sortDescriptor(for: sortField, ascending: false)
        }
        tableView.addTableColumn(column)
    }

    private func sortDescriptor(for field: SortField, ascending: Bool) -> NSSortDescriptor {
        NSSortDescriptor(key: field.rawValue, ascending: ascending)
    }

    // --- 入力 ----------------------------------------------------------------

    func update(
        entries: [ProfileBridge.CatalogEntry],
        selected: Set<String>,
        editable: Bool,
        usageAvailable: Bool
    ) {
        self.entries = entries
        self.selected = selected
        self.editable = editable
        self.usageAvailable = usageAvailable
        refilter()
    }

    func setSelected(_ selected: Set<String>, editable: Bool) {
        self.selected = selected
        self.editable = editable
        // 並べ替えはしない。チェックした瞬間に行が動くと押し間違える。
        tableView.reloadData()
    }

    func applyToolResults(_ results: [ProfileBridge.ToolResult]) {
        let byId = Dictionary(uniqueKeysWithValues: results.map { ($0.id, $0) })
        entries = entries.map { entry in
            guard let result = byId[entry.id] else { return entry }
            return entry.replacingToolState(with: result)
        }
        refilter()
    }

    func entry(id: String) -> ProfileBridge.CatalogEntry? {
        entries.first { $0.id == id }
    }

    private func matches(_ entry: ProfileBridge.CatalogEntry) -> Bool {
        // 選択済みは常に残す。絞り込みで今の選択が消えると、何を保存しようと
        // しているのか画面から分からなくなる。
        if selected.contains(entry.id) {
            return true
        }
        if filters.zdrOnly && !entry.zdrSupported { return false }
        if filters.noTrainingOnly && entry.trainsOnData != false { return false }
        if filters.freeOnly && !entry.free { return false }
        if filters.reasoningOnly && entry.efforts.isEmpty { return false }
        if !filters.showUnsupported && entry.toolSupport == "unsupported" { return false }
        let needle = filters.search.trimmingCharacters(in: .whitespaces).lowercased()
        if !needle.isEmpty {
            return entry.id.lowercased().contains(needle)
                || entry.displayName.lowercased().contains(needle)
        }
        return true
    }

    private func usage(_ entry: ProfileBridge.CatalogEntry) -> Double? {
        guard let raw = entry.usageTokens?[Self.usageWindow] else { return nil }
        return Double(raw)
    }

    private func valueOrdered<T: Comparable>(_ lhs: T?, _ rhs: T?, tie: () -> Bool) -> Bool {
        // 「—」は値が0なのではなく未取得。方向にかかわらず末尾へ送る。
        switch (lhs, rhs) {
        case (nil, nil):
            return tie()
        case (nil, _):
            return false
        case (_, nil):
            return true
        case let (left?, right?):
            if left == right { return tie() }
            return sort.ascending ? left < right : left > right
        }
    }

    private func nameOrdered(
        _ lhs: ProfileBridge.CatalogEntry, _ rhs: ProfileBridge.CatalogEntry
    ) -> Bool {
        let comparison = lhs.displayName.localizedCaseInsensitiveCompare(rhs.displayName)
        if comparison == .orderedSame { return lhs.id < rhs.id }
        return sort.ascending ? comparison == .orderedAscending : comparison == .orderedDescending
    }

    private func tieBreak(
        _ lhs: ProfileBridge.CatalogEntry, _ rhs: ProfileBridge.CatalogEntry
    ) -> Bool {
        let comparison = lhs.displayName.localizedCaseInsensitiveCompare(rhs.displayName)
        if comparison == .orderedSame { return lhs.id < rhs.id }
        return comparison == .orderedAscending
    }

    private func refilter() {
        let filtered = entries.filter(matches)
        let ordered: [ProfileBridge.CatalogEntry]
        switch sort.field {
        case .released:
            ordered = filtered.sorted { lhs, rhs in
                valueOrdered(lhs.created, rhs.created) { tieBreak(lhs, rhs) }
            }
        case .usage:
            ordered = filtered.sorted { lhs, rhs in
                valueOrdered(usage(lhs), usage(rhs)) { tieBreak(lhs, rhs) }
            }
        case .input:
            ordered = filtered.sorted { lhs, rhs in
                valueOrdered(Double(lhs.headline.input), Double(rhs.headline.input)) {
                    tieBreak(lhs, rhs)
                }
            }
        case .output:
            ordered = filtered.sorted { lhs, rhs in
                valueOrdered(Double(lhs.headline.output), Double(rhs.headline.output)) {
                    tieBreak(lhs, rhs)
                }
            }
        case .model:
            ordered = filtered.sorted(by: nameOrdered)
        }
        // 選択済みを先頭へ固める。並べ替えても今選んでいるものが視界から出ない。
        visible = ordered.filter { selected.contains($0.id) }
            + ordered.filter { !selected.contains($0.id) }
        tableView.reloadData()
    }

    var visibleCount: Int { visible.count }
    var unsupportedCount: Int { entries.filter { $0.toolSupport == "unsupported" }.count }

    // --- 描画 ----------------------------------------------------------------

    func numberOfRows(in tableView: NSTableView) -> Int { visible.count }

    @objc private func rowToggled(_ sender: NSButton) {
        guard visible.indices.contains(sender.tag) else { return }
        let entry = visible[sender.tag]
        let wanted = sender.state == .on
        // 実際の反映は呼び出し側が決める。確認を出して取り消す場合があるので、
        // ここでは状態を持たず、表示は次のsetSelectedで揃える。
        sender.state = selected.contains(entry.id) ? .on : .off
        onToggle(entry.id, wanted)
    }

    private func label(_ text: String, mono: Bool = false, secondary: Bool = false) -> NSTextField {
        let field = NSTextField(labelWithString: text)
        field.font = mono
            ? .monospacedDigitSystemFont(ofSize: NSFont.smallSystemFontSize, weight: .regular)
            : .systemFont(ofSize: NSFont.smallSystemFontSize)
        if secondary {
            field.textColor = .secondaryLabelColor
        }
        field.lineBreakMode = .byTruncatingTail
        return field
    }

    private func released(_ entry: ProfileBridge.CatalogEntry) -> String {
        guard let created = entry.created else { return "—" }
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter.string(from: Date(timeIntervalSince1970: created))
    }

    private func usageText(_ entry: ProfileBridge.CatalogEntry) -> String {
        guard usageAvailable else { return "—" }
        guard let raw = entry.usageTokens?[Self.usageWindow], let value = Double(raw) else {
            // トップ50圏外。0ではなく「データなし」なので区別して出す。
            return "—"
        }
        let units: [(Double, String)] = [(1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")]
        for (scale, suffix) in units where value >= scale {
            return String(format: "%.1f%@", value / scale, suffix)
        }
        return String(format: "%.0f", value)
    }

    private func badges(_ entry: ProfileBridge.CatalogEntry) -> NSView {
        var parts: [String] = []
        parts.append(entry.zdrSupported ? "ZDR" : "ZDRなし")
        if entry.free { parts.append("無料") }
        if entry.trainsOnData == nil && !entry.zdrSupported { parts.append("学習不明") }
        if entry.trainsOnData == true { parts.append("学習あり") }
        if !entry.efforts.isEmpty { parts.append("reasoning") }

        let field = label(parts.joined(separator: " · "), secondary: entry.zdrSupported)
        if !entry.zdrSupported {
            // 安全性が下がる行なので、二次色へ沈めない。
            field.textColor = .systemOrange
        }
        return field
    }

    private func toolLabel(_ entry: ProfileBridge.CatalogEntry) -> NSTextField {
        let labels = [
            "verified": "検証済み",
            "partial": "一部対応",
            "declared": "公称",
            "unknown": "不明",
            "unsupported": "非対応",
        ]
        let status = entry.toolSupport ?? "unknown"
        var lines = [labels[status] ?? "不明"]
        if let provider = entry.toolProvider, !provider.isEmpty {
            lines.append(provider)
        }
        let field = label(lines.joined(separator: "\n"), secondary: status == "declared")
        field.maximumNumberOfLines = 2
        var details = [entry.toolSupportReason ?? "互換性の根拠はありません。"]
        if let provider = entry.toolProvider, !provider.isEmpty {
            var providerLine = "検証provider: \(provider)"
            if let attempt = entry.toolProviderAttempt {
                providerLine += "（試行 \(attempt)）"
            }
            details.append(providerLine)
        }
        if let verifiedAt = entry.toolVerifiedAt {
            let formatter = DateFormatter()
            formatter.dateFormat = "yyyy-MM-dd HH:mm"
            details.append("検証時刻: \(formatter.string(from: Date(timeIntervalSince1970: verifiedAt)))")
        }
        field.toolTip = details.joined(separator: "\n")
        if status == "partial" || status == "unsupported" {
            field.textColor = .systemOrange
        }
        return field
    }

    func tableView(
        _ tableView: NSTableView, viewFor column: NSTableColumn?, row: Int
    ) -> NSView? {
        guard visible.indices.contains(row), let identifier = column?.identifier.rawValue else {
            return nil
        }
        let entry = visible[row]
        switch identifier {
        case "pick":
            let checkbox = NSButton(checkboxWithTitle: "", target: self, action: #selector(rowToggled))
            checkbox.tag = row
            checkbox.state = selected.contains(entry.id) ? .on : .off
            checkbox.isEnabled = editable
            return checkbox
        case "model":
            let name = label(entry.displayName)
            let slug = label(entry.id, secondary: true)
            slug.font = .systemFont(ofSize: NSFont.smallSystemFontSize - 1)
            let stack = NSStackView(views: [name, slug])
            stack.orientation = .vertical
            stack.alignment = .leading
            stack.spacing = 0
            return stack
        case "input":
            return label(entry.headline.input, mono: true)
        case "output":
            return label(entry.headline.output, mono: true)
        case "released":
            return label(released(entry), mono: true, secondary: true)
        case "usage":
            return label(usageText(entry), mono: true, secondary: true)
        case "tool":
            return toolLabel(entry)
        case "badges":
            return badges(entry)
        default:
            return nil
        }
    }

    func tableView(_ tableView: NSTableView, shouldSelectRow row: Int) -> Bool { false }

    func tableView(
        _ tableView: NSTableView, sortDescriptorsDidChange oldDescriptors: [NSSortDescriptor]
    ) {
        guard let descriptor = tableView.sortDescriptors.first,
              let key = descriptor.key,
              let field = SortField(rawValue: key) else { return }
        sort = Sort(field: field, ascending: descriptor.ascending)
        refilter()
    }
}
