import AppIntents
import AppKit
import SwiftUI
import WidgetKit

/// TUI tokens that resolve against the widget's drawing appearance.
/// WidgetKit renders light and dark snapshots; NSColor providers also
/// flip if the desktop widget sits on a light vs dark wallpaper region.
enum Palette {
    static let fg = Color.openswap(light: 0x2B2723, dark: 0xE8E4DE)
    static let muted = Color.openswap(light: 0x635D55, dark: 0x8A8A8A)
    static let accent = Color.openswap(light: 0x954C2A, dark: 0xD7875F)
    static let ok = Color.openswap(light: 0x3D6B3D, dark: 0x87AF87)
    static let warn = Color.openswap(light: 0x795911, dark: 0xD7AF5F)
    static let crit = Color.openswap(light: 0xAD3128, dark: 0xD75F5F)
    static let track = Color.openswap(light: 0xCEC7BA, dark: 0x3A3A3A)

    static func severity(_ pct: Double) -> Color {
        if pct >= 90 { return crit }
        if pct >= 70 { return warn }
        return ok
    }
}

extension Color {
    static func openswap(
        light: UInt32,
        dark: UInt32,
        lightOpacity: Double = 1,
        darkOpacity: Double = 1
    ) -> Color {
        Color(nsColor: NSColor(name: nil) { appearance in
            let isDark = appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
            let hex = isDark ? dark : light
            let opacity = isDark ? darkOpacity : lightOpacity
            return NSColor(
                srgbRed: Double((hex >> 16) & 0xFF) / 255,
                green: Double((hex >> 8) & 0xFF) / 255,
                blue: Double(hex & 0xFF) / 255,
                alpha: opacity
            )
        })
    }
}

struct OpenSwapWidgetView: View {
    var entry: OpenSwapEntry
    @Environment(\.widgetFamily) private var family

    var body: some View {
        VStack(alignment: .leading, spacing: family == .systemSmall ? 6 : 8) {
            content
            if let footer = staleFooter {
                Spacer(minLength: 0)
                Text(footer)
                    .font(.system(size: 10))
                    .foregroundStyle(Palette.muted)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .containerBackground(.background, for: .widget)
    }

    @ViewBuilder
    private var content: some View {
        if entry.snapshot == nil {
            emptyState()
        } else if entry.configuration.layout == .combined {
            combinedContent
        } else {
            accountList(visibleAccounts)
        }
    }

    @ViewBuilder
    private var combinedContent: some View {
        let windows = combinedWindows
        let rows = visibleAccounts
        if windows.isEmpty && rows.isEmpty {
            emptyState()
        } else {
            VStack(alignment: .leading, spacing: family == .systemSmall ? 6 : 8) {
                if !windows.isEmpty {
                    CombinedBlock(
                        windows: windows,
                        now: entry.date,
                        compact: family == .systemSmall,
                        switchNum: windows.first?.switchNum
                    )
                }
                if !rows.isEmpty {
                    if !windows.isEmpty {
                        Divider().opacity(0.35)
                    }
                    accountList(rows)
                }
            }
        }
    }

    @ViewBuilder
    private func accountList(_ accounts: [AccountCard]) -> some View {
        if accounts.isEmpty {
            if entry.configuration.layout == .one, entry.configuration.account != nil {
                missingAccountState()
            } else {
                emptyState()
            }
        } else {
            VStack(alignment: .leading, spacing: family == .systemSmall ? 6 : 8) {
                ForEach(Array(accounts.enumerated()), id: \.element.id) { index, account in
                    if index > 0 {
                        Divider().opacity(0.35)
                    }
                    accountBlock(for: account)
                }
            }
        }
    }

    @ViewBuilder
    private func accountBlock(for account: AccountCard) -> some View {
        let block = AccountBlock(
            account: account,
            now: entry.date,
            compact: family == .systemSmall,
            maxWindows: maxWindows
        )
        if account.disabled {
            block
        } else {
            Button(intent: SwitchAccountIntent(num: account.num)) {
                block
            }
            .buttonStyle(.plain)
        }
    }

    private var isSmall: Bool { family == .systemSmall }

    private var primaryLabel: String {
        entry.configuration.windows == .sevenDay ? "7d" : "5h"
    }

    private var accountCap: Int {
        switch family {
        case .systemSmall: return 2
        case .systemMedium: return 3
        case .systemLarge: return 6
        case .systemExtraLarge: return 10
        default: return 3
        }
    }

    private var maxWindows: Int {
        if isSmall {
            return entry.configuration.windows == .both ? 2 : 1
        }
        if family == .systemMedium { return 3 }
        return 6
    }

    private var visibleAccounts: [AccountCard] {
        let all = entry.snapshot?.accounts ?? []
        switch entry.configuration.layout {
        case .one:
            if let id = entry.configuration.account?.id,
               let match = all.first(where: { $0.num == id }) {
                return [withFilteredWindows(match)]
            }
            return pickByRemaining(all, limit: 1).map(withFilteredWindows)
        case .combined:
            if isSmall { return [] }
            return Array(all.filter { !$0.disabled }.prefix(accountCap)).map(withFilteredWindows)
        case .all:
            if isSmall {
                return pickByRemaining(all, limit: 2).map(withFilteredWindows)
            }
            return Array(all.prefix(accountCap)).map(withFilteredWindows)
        }
    }

    private var combinedWindows: [CombinedWindow] {
        guard let snapshot = entry.snapshot else { return [] }
        let maps = snapshot.combinedMaps(now: entry.date)
        switch entry.configuration.windows {
        case .fiveHour:
            return [maps.fiveHour].compactMap { $0 }
        case .sevenDay:
            return [maps.sevenDay].compactMap { $0 }
        case .both:
            return [maps.fiveHour, maps.sevenDay].compactMap { $0 }
        }
    }

    private var staleFooter: String? {
        guard let snapshot = entry.snapshot else { return nil }
        return updatedFooter(updatedAt: snapshot.updatedAt, now: entry.date)
    }

    private func filterWindows(_ windows: [UsageWindow]) -> [UsageWindow] {
        switch entry.configuration.windows {
        case .fiveHour:
            return windows.filter { $0.label == "5h" }
        case .sevenDay:
            return windows.filter { $0.label == "7d" }
        case .both:
            if family == .systemLarge || family == .systemExtraLarge {
                return windows
            }
            return windows.filter { $0.label == "5h" || $0.label == "7d" }
        }
    }

    private func withFilteredWindows(_ account: AccountCard) -> AccountCard {
        var copy = account
        copy.windows = filterWindows(account.windows)
        return copy
    }

    private func pickByRemaining(_ accounts: [AccountCard], limit: Int) -> [AccountCard] {
        let enabled = accounts.filter { !$0.disabled }
        let pool = enabled.isEmpty ? accounts : enabled
        func score(_ card: AccountCard) -> Double {
            if card.needsRelogin == true { return -1 }
            guard let window = card.windows.first(where: { $0.label == primaryLabel }) else {
                return -2
            }
            return 100 - window.pct
        }
        return Array(pool.sorted { score($0) > score($1) }.prefix(limit))
    }

    private func emptyState() -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("openswap")
                .font(.headline)
                .foregroundStyle(Palette.fg)
            Text("Waiting for the menu bar extra. Run openswap menubar, then this fills in.")
                .font(.caption)
                .foregroundStyle(Palette.muted)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func missingAccountState() -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("openswap")
                .font(.headline)
                .foregroundStyle(Palette.fg)
            Text("That account is not in openswap anymore. Right-click the widget to pick another.")
                .font(.caption)
                .foregroundStyle(Palette.muted)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

private struct CombinedBlock: View {
    var windows: [CombinedWindow]
    var now: Date
    var compact: Bool
    var switchNum: String?

    var body: some View {
        let content = VStack(alignment: .leading, spacing: compact ? 4 : 6) {
            ForEach(windows, id: \.label) { window in
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text("\(window.label) left")
                        .font(.system(size: compact ? 12 : 13, weight: .semibold))
                        .foregroundStyle(Palette.fg)
                    Spacer(minLength: 4)
                    Text(String(format: "%.1f / %d", window.remaining, window.total))
                        .font(.system(size: compact ? 13 : 15, weight: .semibold).monospacedDigit())
                        .foregroundStyle(Palette.fg)
                }
            }
            if let primary = windows.first {
                RemainingStack(slices: primary.slices, total: max(primary.total, 1))
                    .frame(height: compact ? 6 : 8)
                if let line = combinedSubline(primary, now: now) {
                    Text(line)
                        .font(.caption)
                        .foregroundStyle(Palette.muted)
                        .lineLimit(1)
                }
            }
        }
        if let num = switchNum, !num.isEmpty {
            Button(intent: SwitchAccountIntent(num: num)) {
                content
            }
            .buttonStyle(.plain)
        } else {
            content
        }
    }
}

private func combinedSubline(_ window: CombinedWindow, now: Date) -> String? {
    var parts: [String] = []
    if let title = window.hottestTitle {
        if let slice = window.slices.first(where: { $0.num == window.hottestNum }) {
            parts.append("\(title) \(Int(slice.pct.rounded()))%")
        } else {
            parts.append(title)
        }
    }
    if let text = liveCountdown(
        resetsAtTs: window.nextResetsAtTs,
        now: now,
        fallback: window.nextCountdown
    ) {
        if window.nextNum != nil, window.nextNum != window.hottestNum {
            parts.append("next \(text)")
        } else {
            parts.append(text)
        }
    }
    return parts.isEmpty ? nil : parts.joined(separator: " · ")
}

private struct RemainingStack: View {
    var slices: [CombinedSlice]
    var total: Int

    var body: some View {
        GeometryReader { geo in
            let unit = geo.size.width / CGFloat(max(total, 1))
            ZStack(alignment: .leading) {
                Capsule().fill(Palette.track)
                HStack(spacing: 1) {
                    ForEach(slices) { slice in
                        if slice.remaining > 0 {
                            Capsule()
                                .fill(Palette.severity(slice.pct))
                                .frame(width: max(2, unit * CGFloat(slice.remaining)))
                        }
                    }
                    Spacer(minLength: 0)
                }
            }
            .clipShape(Capsule())
        }
    }
}

private struct AccountBlock: View {
    var account: AccountCard
    var now: Date
    var compact: Bool
    var maxWindows: Int

    var body: some View {
        VStack(alignment: .leading, spacing: compact ? 3 : 4) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                if account.active {
                    RoundedRectangle(cornerRadius: 1, style: .continuous)
                        .fill(Palette.accent)
                        .frame(width: 3, height: 12)
                }
                Text(account.title)
                    .font(.system(size: compact ? 12 : 13, weight: .semibold))
                    .foregroundStyle(Palette.fg)
                    .lineLimit(1)
                Spacer(minLength: 4)
                if account.provider == "codex" || account.num.hasPrefix("codex:") {
                    Text("Codex")
                        .font(.system(size: 10, weight: .medium))
                        .foregroundStyle(Palette.muted)
                }
                if account.active {
                    Text("active")
                        .font(.system(size: 10, weight: .medium))
                        .foregroundStyle(Palette.accent)
                } else if account.disabled {
                    Text("disabled")
                        .font(.system(size: 10, weight: .medium))
                        .foregroundStyle(Palette.muted)
                }
            }
            if !account.subtitle.isEmpty {
                Text(account.subtitle)
                    .font(.caption)
                    .foregroundStyle(Palette.muted)
                    .lineLimit(1)
            }
            ForEach(Array(account.windows.prefix(maxWindows))) { window in
                WindowRow(
                    window: window,
                    now: now,
                    compact: compact,
                    stale: account.needsRelogin == true
                )
            }
            if let note = account.note {
                Text(note)
                    .font(.caption)
                    .foregroundStyle(Palette.muted)
                    .lineLimit(1)
            }
        }
        .opacity(account.disabled ? 0.45 : 1)
    }
}

private struct WindowRow: View {
    var window: UsageWindow
    var now: Date
    var compact: Bool
    var stale: Bool = false

    var body: some View {
        let color = stale ? Palette.muted : Palette.severity(window.pct)
        let suffix: String = {
            if stale {
                if let text = liveCountdown(resetsAtTs: window.resetsAtTs, now: now, fallback: window.countdown) {
                    return text
                }
                return ""
            }
            if window.maxed { return "max" }
            if let text = liveCountdown(resetsAtTs: window.resetsAtTs, now: now, fallback: window.countdown) {
                return text
            }
            if window.ahead { return "ahead" }
            return ""
        }()
        HStack(spacing: 6) {
            Text(window.label)
                .font(.system(size: compact ? 10 : 11, weight: .medium).monospacedDigit())
                .foregroundStyle(Palette.muted)
                .frame(width: compact ? 32 : 40, alignment: .leading)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
            UsageBar(pct: window.pct, fill: color, track: Palette.track)
                .frame(height: 5)
            Text("\(Int(window.pct.rounded()))%")
                .font(.system(size: compact ? 10 : 11, weight: .medium).monospacedDigit())
                .foregroundStyle(color)
                .frame(width: compact ? 32 : 36, alignment: .trailing)
            Text(suffix)
                .font(.system(size: compact ? 10 : 11).monospacedDigit())
                .foregroundStyle(Palette.muted)
                .frame(width: compact ? 44 : 52, alignment: .trailing)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
        }
    }
}

private struct UsageBar: View {
    var pct: Double
    var fill: Color
    var track: Color

    var body: some View {
        GeometryReader { geo in
            let fraction = min(max(pct, 0), 100) / 100
            ZStack(alignment: .leading) {
                Capsule().fill(track)
                Capsule()
                    .fill(fill)
                    .frame(width: geo.size.width * fraction)
            }
        }
    }
}
