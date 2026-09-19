import Darwin
import Foundation

struct WidgetSnapshot: Codable {
    var schema: Int
    var updatedAt: Double
    var accounts: [AccountCard]
    var combined: CombinedMaps?
}

struct CombinedMaps: Codable {
    var fiveHour: CombinedWindow?
    var sevenDay: CombinedWindow?
}

struct CombinedWindow: Codable {
    var label: String
    var remaining: Double
    var total: Int
    var hottestTitle: String?
    var hottestNum: String?
    var switchNum: String?
    var nextNum: String?
    var nextResetsAtTs: Double?
    var nextCountdown: String?
    var slices: [CombinedSlice]
}

struct CombinedSlice: Codable, Identifiable {
    var num: String
    var title: String
    var pct: Double
    var remaining: Double
    var resetsAtTs: Double?
    var countdown: String?
    var id: String { num }
}

struct AccountCard: Codable, Identifiable {
    var num: String
    var title: String
    var subtitle: String
    var active: Bool
    var disabled: Bool
    var note: String?
    var needsRelogin: Bool?
    var windows: [UsageWindow]
    var provider: String? = nil
    var id: String { num }
}

struct UsageWindow: Codable, Identifiable {
    var label: String
    var pct: Double
    var countdown: String?
    var resetsAtTs: Double?
    var ahead: Bool
    var maxed: Bool
    var id: String { label }
}

enum SnapshotStore {
    static var fileURL: URL {
        realHomeDirectory()
            .appendingPathComponent("Library/Application Support/OpenSwap/widget-snapshot.json")
    }

    static var legacyFileURL: URL {
        realHomeDirectory()
            .appendingPathComponent("Library/Application Support/cswap/widget-snapshot.json")
    }

    static func load() -> WidgetSnapshot? {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        for url in [fileURL, legacyFileURL] {
            if let data = try? Data(contentsOf: url),
               let snapshot = try? decoder.decode(WidgetSnapshot.self, from: data) {
                return snapshot
            }
        }
        return nil
    }
}

func realHomeDirectory() -> URL {
    if let pw = getpwuid(getuid()), let dir = pw.pointee.pw_dir {
        return URL(fileURLWithPath: String(cString: dir))
    }
    return FileManager.default.homeDirectoryForCurrentUser
}

func liveCountdown(resetsAtTs: Double?, now: Date, fallback: String?) -> String? {
    guard let ts = resetsAtTs else { return fallback }
    let remaining = Int(ts - now.timeIntervalSince1970)
    if remaining <= 0 { return nil }
    let days = remaining / 86_400
    let hours = (remaining % 86_400) / 3_600
    let minutes = (remaining % 3_600) / 60
    if days > 0 { return "\(days)d \(hours)h" }
    if hours > 0 { return "\(hours)h \(minutes)m" }
    return "\(minutes)m"
}

func remainingFraction(pct: Double) -> Double {
    min(1, max(0, (100 - pct) / 100))
}

extension CombinedWindow {
    static func from(accounts: [AccountCard], label: String, now: Date) -> CombinedWindow? {
        var slices: [CombinedSlice] = []
        for card in accounts {
            if card.disabled { continue }
            if card.needsRelogin == true { continue }
            guard let window = card.windows.first(where: { $0.label == label }) else { continue }
            slices.append(
                CombinedSlice(
                    num: card.num,
                    title: card.title,
                    pct: window.pct,
                    remaining: remainingFraction(pct: window.pct),
                    resetsAtTs: window.resetsAtTs,
                    countdown: window.countdown
                )
            )
        }
        guard !slices.isEmpty else { return nil }
        let remaining = slices.reduce(0) { $0 + $1.remaining }
        // First slice wins a tie, matching the Python snapshot helper.
        var hottest = slices[0]
        var switcher = slices[0]
        for slice in slices.dropFirst() {
            if slice.pct > hottest.pct { hottest = slice }
            if slice.remaining > switcher.remaining { switcher = slice }
        }
        let nowTs = now.timeIntervalSince1970
        let upcoming = slices.filter { ($0.resetsAtTs ?? 0) > nowTs }
        let next = upcoming.min(by: { ($0.resetsAtTs ?? .infinity) < ($1.resetsAtTs ?? .infinity) })
        return CombinedWindow(
            label: label,
            remaining: remaining,
            total: slices.count,
            hottestTitle: hottest.title,
            hottestNum: hottest.num,
            switchNum: switcher.num,
            nextNum: next?.num,
            nextResetsAtTs: next?.resetsAtTs,
            nextCountdown: next?.countdown,
            slices: slices
        )
    }
}

extension CombinedMaps {
    static func from(accounts: [AccountCard], now: Date) -> CombinedMaps {
        CombinedMaps(
            fiveHour: CombinedWindow.from(accounts: accounts, label: "5h", now: now),
            sevenDay: CombinedWindow.from(accounts: accounts, label: "7d", now: now)
        )
    }
}

extension WidgetSnapshot {
    func combinedMaps(now: Date) -> CombinedMaps {
        if let combined, combined.fiveHour != nil || combined.sevenDay != nil {
            return combined
        }
        return CombinedMaps.from(accounts: accounts, now: now)
    }
}

func updatedFooter(updatedAt: Double, now: Date, staleAfter: TimeInterval = 10 * 60) -> String? {
    let age = now.timeIntervalSince1970 - updatedAt
    if age < staleAfter { return nil }
    let minutes = Int(age / 60)
    if minutes < 1 { return "Updated just now" }
    if minutes < 60 { return "Updated \(minutes)m ago" }
    let hours = minutes / 60
    if hours < 48 { return "Updated \(hours)h ago" }
    return "Updated \(hours / 24)d ago"
}

extension WidgetSnapshot {
    static var sample: WidgetSnapshot {
        let now = Date().timeIntervalSince1970
        let personal = AccountCard(
            num: "1",
            title: "personal",
            subtitle: "you@example.com",
            active: true,
            disabled: false,
            note: nil,
            needsRelogin: nil,
            windows: [
                UsageWindow(
                    label: "5h", pct: 20, countdown: "2h 10m",
                    resetsAtTs: now + 7_800, ahead: false, maxed: false
                ),
                UsageWindow(
                    label: "7d", pct: 10, countdown: "3d 21h",
                    resetsAtTs: now + 334_800, ahead: false, maxed: false
                ),
            ]
        )
        let ads = AccountCard(
            num: "2",
            title: "Ads Online",
            subtitle: "you@example.com",
            active: false,
            disabled: false,
            note: nil,
            needsRelogin: nil,
            windows: [
                UsageWindow(
                    label: "5h", pct: 80, countdown: "4h 0m",
                    resetsAtTs: now + 14_400, ahead: false, maxed: false
                ),
                UsageWindow(
                    label: "7d", pct: 40, countdown: "4d 2h",
                    resetsAtTs: now + 352_800, ahead: false, maxed: false
                ),
            ]
        )
        let accounts = [personal, ads]
        return WidgetSnapshot(
            schema: 1,
            updatedAt: now,
            accounts: accounts,
            combined: CombinedMaps.from(accounts: accounts, now: Date(timeIntervalSince1970: now))
        )
    }
}
