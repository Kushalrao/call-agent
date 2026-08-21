import SwiftUI

/// The flight results list — Figma node 171:2813.
///
/// Rows live inside the same frosted card as the rest of the call, so this is the
/// card's contents rather than a panel of its own.
///
/// Two asset departures, both declared rather than hidden:
///
/// - **The airline logo is content, not a design asset.** The design's Lufthansa
///   and Emirates marks stand in for whoever actually flies the route, so real
///   logos are loaded from a CDN keyed by airline IATA code. The designed geometry
///   is preserved exactly (36.95pt square, 3.79pt radius), and an airline we
///   cannot identify falls back to initials rather than to another airline's mark.
/// - **The arrow between the times is drawn, not exported.** It is a geometric
///   connector — a line with a dot — rather than an icon with a glyph, and its
///   designed box (54 × 17.06) is reproduced exactly. An exported SVG would need
///   a rendering dependency this project does not have.
private enum Row {
    static let height: CGFloat = 78.644
    static let width: CGFloat = 343
    static let listInsetX: CGFloat = 7
    static let listInsetY: CGFloat = 8.42

    static let logoSize: CGFloat = 36.953
    static let logoRadius: CGFloat = 3.79
    static let logoX: CGFloat = 6.63
    static let logoY: CGFloat = 18.95

    static let contentX: CGFloat = 55.9
    static let contentY: CGFloat = 18
    static let contentWidth: CGFloat = 274.779

    /// The depart/arrow/arrive group. Narrower than the content block, which is
    /// what leaves the price sitting out to the right.
    static let timesWidth: CGFloat = 174.343
    static let arrowWidth: CGFloat = 54.008
    static let arrowHeight: CGFloat = 17.055

    static let timeFont = Font.system(size: 16.108, weight: .semibold)
    static let subtitleFont = Font.system(size: 13.265, weight: .medium)
    static let priceFont = Font.system(size: 16.108, weight: .semibold)
    /// Colors/Green in the design.
    static let priceColor = Color(red: 52 / 255, green: 199 / 255, blue: 89 / 255)
    static let subtitleOpacity: Double = 0.6
    static let subtitleY: CGFloat = 25.58
}

/// One flight, as the results card shows it.
///
/// Named apart from `FlightRow` in WidgetViews on purpose: that one is the
/// two-party call's flight card, a different design for a different surface, and
/// collapsing them would couple two things that only look similar.
struct ResultRow: View {
    let flight: Flight

    var body: some View {
        ZStack(alignment: .topLeading) {
            Color.clear.frame(width: Row.width, height: Row.height)

            AirlineMark(carrier: flight.carrier, logo: flight.logoURL)
                .offset(x: Row.logoX, y: Row.logoY)

            ZStack(alignment: .topLeading) {
                Color.clear.frame(width: Row.contentWidth, height: 42.638)

                HStack(spacing: 0) {
                    Text(flight.departsClock ?? flight.departs ?? "—")
                        .font(Row.timeFont)
                        .foregroundStyle(.black)
                        .fixedSize()
                    Spacer(minLength: 0)
                    Connector()
                        .frame(width: Row.arrowWidth, height: Row.arrowHeight)
                    Spacer(minLength: 0)
                    Text(flight.arrivesClock ?? flight.arrives ?? "—")
                        .font(Row.timeFont)
                        .foregroundStyle(.black)
                        .fixedSize()
                }
                .frame(width: Row.timesWidth)

                Text(flight.stopsLabel)
                    .font(Row.subtitleFont)
                    .foregroundStyle(.black.opacity(Row.subtitleOpacity))
                    .offset(y: Row.subtitleY)

                Text(flight.priceLabel)
                    .font(Row.priceFont)
                    .foregroundStyle(Row.priceColor)
                    .frame(width: Row.contentWidth, alignment: .trailing)
            }
            .offset(x: Row.contentX, y: Row.contentY)
        }
        .frame(width: Row.width, height: Row.height)
    }
}

/// The line-and-dot between the two times (171:2823).
private struct Connector: View {
    var body: some View {
        GeometryReader { geo in
            let midY = geo.size.height / 2
            Path { path in
                path.move(to: CGPoint(x: 0, y: midY))
                path.addLine(to: CGPoint(x: geo.size.width, y: midY))
            }
            .stroke(.black, lineWidth: 1.4)

            Circle()
                .fill(.black)
                .frame(width: 5, height: 5)
                .position(x: geo.size.width / 2, y: midY)
        }
    }
}

/// The carrier's logo, with initials while it loads or when the airline is
/// unknown.
///
/// The design ships Lufthansa and Emirates marks as placeholders. Real logos come
/// from a logo CDN keyed by airline IATA code, which is why the code is derived
/// server-side (agent/airlines.py) rather than guessed from a display name here.
///
/// Initials are the fallback rather than a generic plane, because they are
/// obviously a placeholder — whereas some other airline's mark beside a real fare
/// is a claim the user cannot check.
private struct AirlineMark: View {
    let carrier: String
    let logo: URL?

    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: Row.logoRadius, style: .continuous)
                .fill(Color.white)

            if let logo {
                AsyncImage(url: logo) { phase in
                    switch phase {
                    case let .success(image):
                        image
                            .resizable()
                            .aspectRatio(contentMode: .fit)
                            .padding(2)
                    default:
                        initials
                    }
                }
            } else {
                initials
            }
        }
        .frame(width: Row.logoSize, height: Row.logoSize)
        .clipShape(RoundedRectangle(cornerRadius: Row.logoRadius, style: .continuous))
        .accessibilityLabel(carrier)
    }

    private var initials: some View {
        ZStack {
            Color(red: 236 / 255, green: 236 / 255, blue: 228 / 255)
            Text(monogram)
                .font(.system(size: 13, weight: .bold))
                .foregroundStyle(.black.opacity(0.55))
        }
    }

    private var monogram: String {
        // First carrier only: a codeshare comes through as "Air India, Thai".
        let primary = carrier.split(separator: ",").first.map(String.init) ?? carrier
        let words = primary.split(separator: " ").prefix(2)
        if words.count >= 2 {
            return words.compactMap(\.first).map(String.init).joined().uppercased()
        }
        // A single-word carrier: first two letters, not one. "IndiGo" showed as "I".
        let single = words.first.map(String.init) ?? primary
        return String(single.prefix(2)).uppercased()
    }
}

/// The list.
///
/// `embedded` means it is already inside a scroll view — nesting two scroll views
/// on the same axis makes both feel broken, and the weather card sits above these
/// rows in one shared scroll.
struct FlightList: View {
    let flights: [Flight]
    var embedded = false

    var body: some View {
        if embedded {
            rows
        } else {
            ScrollView(.vertical, showsIndicators: false) { rows }
        }
    }

    private var rows: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(flights) { flight in
                ResultRow(flight: flight)
            }
        }
        .frame(width: Row.width, alignment: .leading)
        .padding(.leading, Row.listInsetX)
        .padding(.top, Row.listInsetY)
    }
}
