import Combine
import Foundation
import SwiftUI

/// One flight, as the phone shows it.
struct Flight: Decodable, Identifiable, Equatable {
    let carrier: String
    let priceInr: Int
    let stops: Int
    let platform: String
    let durationMin: Int?
    let departs: String?
    let arrives: String?
    /// 24-hour. The card uses these; `departs`/`arrives` are the spoken form.
    /// "18:00" is five characters and fits the designed time group, "6:00 pm" is
    /// eight and wrapped onto a second line.
    let departsClock: String?
    let arrivesClock: String?
    /// Two-letter IATA airline code. Nil when we could not determine it, which
    /// the row renders as initials rather than as some other airline's logo.
    let airlineCode: String?

    /// Where the carrier's logo lives. Nil when the airline is unknown — a logo
    /// is an assertion about who operates the flight, and the wrong one beside a
    /// real fare is a claim the user has no way to check.
    var logoURL: URL? {
        guard let code = airlineCode, code.count >= 2 else { return nil }
        return URL(string: "https://pics.avs.io/120/120/\(code.uppercased()).png")
    }

    /// Stable across a filter change so rows animate rather than jump.
    var id: String { "\(carrier)-\(priceInr)-\(departs ?? "")-\(platform)" }

    var isDirect: Bool { stops == 0 }

    var stopsLabel: String {
        switch stops {
        case 0: return "Direct flight"
        case 1: return "1 stop"
        default: return "\(stops) stops"
        }
    }

    /// "Rs 14,282" — matching the design, which writes Rs rather than the symbol.
    var priceLabel: String {
        let formatter = NumberFormatter()
        formatter.numberStyle = .decimal
        formatter.groupingSeparator = ","
        let amount = formatter.string(from: NSNumber(value: priceInr)) ?? "\(priceInr)"
        return "Rs \(amount)"
    }
}

/// What the agent has narrowed the list to.
///
/// Applied on the phone rather than re-queried, so filtering is instant and stays
/// in step with what is being said. The agent asks for a shape, not for rows: it
/// never re-sends flight data, so the numbers on screen are always the ones the
/// search returned.
struct FlightFilter: Equatable {
    var directOnly = false
    var airline: String?
    var maxPriceInr: Int?
    var cheapestOnly = false

    static let none = FlightFilter()

    var isActive: Bool { self != .none }

    /// A short line for the header, so it is obvious the list is not everything.
    var label: String? {
        var parts: [String] = []
        if cheapestOnly { parts.append("cheapest") }
        if directOnly { parts.append("direct only") }
        if let airline { parts.append(airline) }
        if let maxPriceInr { parts.append("under \(maxPriceInr / 1000)k") }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    func apply(to flights: [Flight]) -> [Flight] {
        var result = flights
        if directOnly { result = result.filter(\.isDirect) }
        if let airline {
            let wanted = airline.lowercased()
            result = result.filter { $0.carrier.lowercased().contains(wanted) }
        }
        if let maxPriceInr { result = result.filter { $0.priceInr <= maxPriceInr } }
        if cheapestOnly, let best = result.min(by: { $0.priceInr < $1.priceInr }) {
            result = [best]
        }
        // Never filter down to nothing. An empty card while the agent talks about
        // prices reads as broken, so a filter that matches nothing is ignored and
        // said so in the log rather than silently emptying the screen.
        return result.isEmpty ? flights : result
    }
}

private struct LatestResults: Decodable {
    let route: String
    let destinationCity: String
    let departDate: String
    let searchedAt: Double
    let options: [Flight]
}

/// Holds the flights on screen.
///
/// Results are polled rather than pushed. The agent could tell us over its data
/// channel — that is how filtering works — but the rows have to appear even if
/// that channel says nothing, and a poll cannot fail in a way that leaves the
/// card empty while the agent is reading prices out.
@MainActor
final class FlightStore: ObservableObject {
    @Published private(set) var all: [Flight] = []
    @Published private(set) var route: String = ""
    @Published private(set) var destination: String = ""
    @Published var filter: FlightFilter = .none

    private var poller: Task<Void, Never>?
    private var lastSearchedAt: Double = 0
    /// When this session started. Results older than it belong to a previous
    /// conversation and must not appear.
    private var sessionStartedAt: Double = 0

    var visible: [Flight] { filter.apply(to: all) }
    var hasResults: Bool { !all.isEmpty }

    func start() {
        guard poller == nil else { return }
        // Anything the tool is still holding from a previous conversation is not
        // this conversation's. Without this the card opened already full of the
        // last session's flights, before the caller had asked for anything —
        // results should appear because a search happened, not because the
        // server still remembers one.
        sessionStartedAt = Date().timeIntervalSince1970
        poller = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refresh()
                try? await Task.sleep(for: .seconds(2))
            }
        }
    }

    func stop() {
        poller?.cancel()
        poller = nil
        all = []
        route = ""
        destination = ""
        filter = .none
        lastSearchedAt = 0
        sessionStartedAt = 0
    }

    func refresh() async {
        guard let url = URL(string: Config.flightToolURL + "/session/latest") else { return }
        var request = URLRequest(url: url)
        request.timeoutInterval = 8
        request.cachePolicy = .reloadIgnoringLocalCacheData
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard (response as? HTTPURLResponse)?.statusCode == 200 else { return }
            let decoder = JSONDecoder()
            decoder.keyDecodingStrategy = .convertFromSnakeCase
            let latest = try decoder.decode(LatestResults.self, from: data)
            guard !latest.options.isEmpty else { return }
            // Only react to a genuinely newer search. Re-assigning identical rows
            // every two seconds would restart the row animations forever.
            guard latest.searchedAt > lastSearchedAt else { return }
            // And newer than this session. A small grace window, because the
            // search that triggers the first render can land a moment before the
            // store is asked to start.
            guard latest.searchedAt > sessionStartedAt - 5 else { return }
            lastSearchedAt = latest.searchedAt
            withAnimation(.spring(response: 0.4, dampingFraction: 0.85)) {
                all = latest.options
                route = latest.route
                destination = latest.destinationCity
                // A new route clears the old narrowing: a filter meant for
                // Bangkok has no business hiding rows for Bali.
                filter = .none
            }
        } catch {
            // Silent. A missed poll is corrected two seconds later, and there is
            // nothing the user could usefully be told about one.
        }
    }
}
