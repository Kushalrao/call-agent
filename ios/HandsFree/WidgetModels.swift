import Foundation

/// The agent-to-client widget channel (spec section 7).
///
/// Versioned envelope, and clients MUST ignore unknown `type` values so a newer
/// agent can ship a widget this build has never heard of without breaking it.
/// That is why decoding an unrecognised type throws a specific error the caller
/// treats as "drop and log", not as a failure.

struct WidgetEnvelope: Identifiable {
    let v: Int
    let widgetID: String
    let type: String
    let ttlSeconds: Int
    let content: WidgetContent

    var id: String { widgetID }
}

enum WidgetContent {
    case flightResults(FlightResults)
    case agentStatus(AgentStatus)
}

enum WidgetDecodeError: Error {
    /// Forward compatibility, not a bug: log it and move on.
    case unknownType(String)
    case unsupportedVersion(Int)
}

// MARK: - flight_results

struct FlightResults: Decodable {
    struct Route: Decodable {
        let origin: String
        let destination: String
    }

    struct Price: Decodable {
        let amount: Double
        let currency: String
    }

    struct Option: Decodable, Identifiable {
        let carrier: String
        let flight: String
        /// ISO8601 with the airport's local offset, e.g. 2026-12-07T08:10+05:30.
        let depart: String
        let arrive: String
        let stops: Int
        let durationMin: Int
        let price: Price
        let deeplink: String?

        var id: String { "\(carrier)-\(flight)-\(depart)" }
    }

    let route: Route
    let dateRange: [String]
    let options: [Option]
}

// MARK: - agent_status

struct AgentStatus: Decodable {
    /// "thinking" | "searching" | "error" — rendered as a transient toast.
    let state: String
    let message: String
}

// MARK: - Decoding

extension WidgetEnvelope {
    private struct Header: Decodable {
        let v: Int
        let widgetId: String
        let type: String
        let ttlS: Int?
    }

    private struct Typed<P: Decodable>: Decodable {
        let payload: P
    }

    /// Two-pass decode: read the header to learn the type, then decode the
    /// payload for that type. Doing it in one pass would mean an unknown type
    /// failed on the payload shape rather than on the type itself, which is a
    /// much less useful signal.
    static func decode(_ data: Data) throws -> WidgetEnvelope {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase

        let header = try decoder.decode(Header.self, from: data)
        guard header.v == 1 else { throw WidgetDecodeError.unsupportedVersion(header.v) }

        let content: WidgetContent
        switch header.type {
        case "flight_results":
            content = .flightResults(try decoder.decode(Typed<FlightResults>.self, from: data).payload)
        case "agent_status":
            content = .agentStatus(try decoder.decode(Typed<AgentStatus>.self, from: data).payload)
        default:
            throw WidgetDecodeError.unknownType(header.type)
        }

        return WidgetEnvelope(
            v: header.v,
            widgetID: header.widgetId,
            type: header.type,
            ttlSeconds: header.ttlS ?? 300,
            content: content
        )
    }
}

// MARK: - Display helpers
//
// Times arrive as ISO8601 carrying each airport's own UTC offset. Parsing to a
// Date and formatting in the device's timezone would be actively wrong — a
// Bangalore departure must read as Bangalore local time regardless of where the
// phone is. So read the wall-clock straight out of the string.

extension FlightResults.Option {
    var departTime: String { Self.clockTime(from: depart) }
    var arriveTime: String { Self.clockTime(from: arrive) }

    /// True when the flight lands on a later calendar day than it departs.
    var crossesMidnight: Bool {
        guard let d = Self.datePart(depart), let a = Self.datePart(arrive) else { return false }
        return a > d
    }

    var durationText: String {
        let hours = durationMin / 60
        let mins = durationMin % 60
        return mins == 0 ? "\(hours)h" : "\(hours)h \(mins)m"
    }

    var stopsText: String {
        switch stops {
        case 0: return "Direct"
        case 1: return "1 stop"
        default: return "\(stops) stops"
        }
    }

    var priceText: String {
        let formatter = NumberFormatter()
        formatter.numberStyle = .currency
        formatter.currencyCode = price.currency
        formatter.maximumFractionDigits = 0
        return formatter.string(from: NSNumber(value: price.amount))
            ?? "\(price.currency) \(Int(price.amount))"
    }

    private static func clockTime(from iso: String) -> String {
        // 2026-12-07T08:10+05:30 -> "08:10"
        guard let tIndex = iso.firstIndex(of: "T") else { return "--:--" }
        let after = iso[iso.index(after: tIndex)...]
        return String(after.prefix(5))
    }

    private static func datePart(_ iso: String) -> String? {
        iso.split(separator: "T").first.map(String.init)
    }
}

extension FlightResults {
    /// "7 – 13 Dec" from the ISO date range.
    var dateRangeText: String {
        let parts = dateRange.compactMap { Self.shortDate($0) }
        guard parts.count == 2 else { return parts.first ?? "" }
        return "\(parts[0]) – \(parts[1])"
    }

    private static func shortDate(_ iso: String) -> String? {
        let comps = iso.split(separator: "-")
        guard comps.count == 3, let month = Int(comps[1]), let day = Int(comps[2]) else { return nil }
        let names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        guard month >= 1, month <= 12 else { return nil }
        return "\(day) \(names[month - 1])"
    }
}

// MARK: - Sample payload (Phase 1 spike)

#if DEBUG
    enum WidgetSamples {
        /// Byte-identical in shape to what the agent will publish, so the
        /// rendering path being validated here is the real one.
        static let flightResults = """
        {
          "v": 1,
          "widget_id": "\(UUID().uuidString)",
          "type": "flight_results",
          "ttl_s": 300,
          "payload": {
            "route": {"origin": "BLR", "destination": "DPS"},
            "date_range": ["2026-12-07", "2026-12-13"],
            "options": [
              {"carrier": "IndiGo", "flight": "6E 27",
               "depart": "2026-12-07T08:10+05:30", "arrive": "2026-12-07T16:35+08:00",
               "stops": 1, "duration_min": 385,
               "price": {"amount": 24350, "currency": "INR"},
               "deeplink": "https://example.com/book/6E27"},
              {"carrier": "Singapore Airlines", "flight": "SQ 511",
               "depart": "2026-12-07T21:45+05:30", "arrive": "2026-12-08T13:20+08:00",
               "stops": 1, "duration_min": 575,
               "price": {"amount": 31890, "currency": "INR"},
               "deeplink": "https://example.com/book/SQ511"},
              {"carrier": "Malaysia Airlines", "flight": "MH 193",
               "depart": "2026-12-07T23:55+05:30", "arrive": "2026-12-08T14:05+08:00",
               "stops": 1, "duration_min": 490,
               "price": {"amount": 27600, "currency": "INR"},
               "deeplink": null}
            ]
          }
        }
        """.data(using: .utf8)!

        static let searching = """
        {"v": 1, "widget_id": "\(UUID().uuidString)", "type": "agent_status",
         "ttl_s": 20, "payload": {"state": "searching", "message": "Looking up flights to Bali…"}}
        """.data(using: .utf8)!

        /// A type this build does not know. Must be dropped silently, not crash.
        static let unknownType = """
        {"v": 1, "widget_id": "\(UUID().uuidString)", "type": "hotel_results",
         "ttl_s": 60, "payload": {"anything": true}}
        """.data(using: .utf8)!
    }
#endif
