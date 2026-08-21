import Combine
import SwiftUI

/// One day in the forecast.
struct WeatherDay: Decodable, Identifiable, Equatable {
    let date: String
    let weekday: String
    let highC: Int
    let lowC: Int
    let condition: String
    /// An SF Symbol name, chosen server-side from the WMO code so the words and
    /// the picture can never disagree.
    let icon: String

    var id: String { date }
}

private struct LatestWeather: Decodable {
    let city: String
    let condition: String
    let nowC: Int?
    let fetchedAt: Double
    let days: [WeatherDay]
}

/// Holds the forecast on screen.
///
/// Polled like the flights, and for the same reason: the card has to fill because
/// a forecast was fetched, not because the agent remembered to push one.
@MainActor
final class WeatherStore: ObservableObject {
    @Published private(set) var city: String = ""
    @Published private(set) var condition: String = ""
    @Published private(set) var nowC: Int?
    @Published private(set) var days: [WeatherDay] = []

    private var poller: Task<Void, Never>?
    private var lastFetchedAt: Double = 0
    private var sessionStartedAt: Double = 0

    var hasForecast: Bool { !days.isEmpty }

    func start() {
        guard poller == nil else { return }
        // Same session rule as the flights: a forecast from a previous
        // conversation must not be sitting on screen before anyone asked.
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
        city = ""
        condition = ""
        nowC = nil
        days = []
        lastFetchedAt = 0
        sessionStartedAt = 0
    }

    private func refresh() async {
        guard let url = URL(string: Config.flightToolURL + "/session/weather") else { return }
        var request = URLRequest(url: url)
        request.timeoutInterval = 8
        request.cachePolicy = .reloadIgnoringLocalCacheData
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard (response as? HTTPURLResponse)?.statusCode == 200 else { return }
            let decoder = JSONDecoder()
            decoder.keyDecodingStrategy = .convertFromSnakeCase
            let latest = try decoder.decode(LatestWeather.self, from: data)
            #if DEBUG
                print("[weather] got \(latest.days.count) days, fetchedAt=\(latest.fetchedAt) "
                      + "session=\(sessionStartedAt) last=\(lastFetchedAt)")
            #endif
            guard !latest.days.isEmpty,
                  latest.fetchedAt > lastFetchedAt,
                  latest.fetchedAt > sessionStartedAt - 5
            else { return }
            lastFetchedAt = latest.fetchedAt
            withAnimation(.spring(response: 0.42, dampingFraction: 0.85)) {
                city = latest.city
                condition = latest.condition
                nowC = latest.nowC
                days = latest.days
            }
        } catch {
            // A missed poll corrects itself in two seconds, but a *decode* error
            // repeats forever and looks identical to "no forecast yet". Silence
            // here cost a debugging cycle.
            #if DEBUG
                print("[weather] refresh failed: \(error)")
            #endif
        }
    }
}

/// The forecast card.
///
/// Built from the screenshot Kushal supplied: warm gradient, city and condition
/// top-left, then a row of days with the high above the icon and the low below,
/// and two lines running through the highs and the lows.
///
/// Two departures from that screenshot, both deliberate:
///
/// - **Celsius, not Fahrenheit.** The screenshot is a US widget; the destinations
///   here are Asia and the callers are in India.
/// - **No clock and calendar buttons.** Those are the source widget's own
///   controls. Nothing here is tappable — the conversation is the interface, and
///   a control that looks pressable but is not is worse than no control.
struct WeatherCard: View {
    let city: String
    let condition: String
    let nowC: Int?
    let days: [WeatherDay]

    /// Enough for the week; more than eight columns stops being readable at this
    /// width, which is why the API asks for eight.
    private var shown: [WeatherDay] { Array(days.prefix(8)) }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            chart
                .padding(.top, 10)
                .padding(.bottom, 14)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            LinearGradient(
                colors: [
                    Color(red: 232 / 255, green: 96 / 255, blue: 40 / 255),
                    Color(red: 245 / 255, green: 158 / 255, blue: 52 / 255),
                ],
                startPoint: .topLeading,
                endPoint: .bottomTrailing
            )
        )
        .clipShape(RoundedRectangle(cornerRadius: 26, style: .continuous))
    }

    private var header: some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: 1) {
                Text(city)
                    .font(.system(size: 22, weight: .semibold))
                    .foregroundStyle(.white)
                Text(condition)
                    .font(.system(size: 17, weight: .regular))
                    // Lighter than the city, as in the screenshot: the place is
                    // the heading and the condition is the subtitle.
                    .foregroundStyle(.white.opacity(0.72))
            }
            Spacer()
            if let nowC {
                Text("\(nowC)°")
                    .font(.system(size: 26, weight: .semibold))
                    .foregroundStyle(.white)
            }
        }
        .padding(.horizontal, 18)
        .padding(.top, 16)
    }

    private var chart: some View {
        // A grid rather than an HStack of VStacks, so the highs, icons and lows
        // each line up across columns. Stacked columns let a two-digit high shift
        // its own icon out of line with its neighbours.
        GeometryReader { geo in
            let columnWidth = geo.size.width / CGFloat(max(shown.count, 1))
            let highs = normalised(shown.map(\.highC), in: geo.size, top: true)
            let lows = normalised(shown.map(\.lowC), in: geo.size, top: false)

            ZStack {
                polyline(through: highs)
                polyline(through: lows)

                ForEach(Array(shown.enumerated()), id: \.element.id) { index, day in
                    column(day, isToday: index == 0)
                        .frame(width: columnWidth)
                        .position(
                            x: columnWidth * (CGFloat(index) + 0.5),
                            y: geo.size.height / 2
                        )
                }
            }
        }
        .frame(height: 132)
    }

    private func column(_ day: WeatherDay, isToday: Bool) -> some View {
        VStack(spacing: 3) {
            Text("\(day.highC)°")
                .font(.system(size: 16, weight: .semibold))
            Image(systemName: day.icon)
                .font(.system(size: 21))
                .symbolRenderingMode(.hierarchical)
                .frame(height: 26)
            Text("\(day.lowC)°")
                .font(.system(size: 16, weight: .regular))
                .foregroundStyle(.white.opacity(0.82))
            Text(day.weekday)
                .font(.system(size: 13, weight: isToday ? .bold : .regular))
                .foregroundStyle(.white.opacity(isToday ? 1 : 0.8))
                .padding(.top, 2)
        }
        .foregroundStyle(.white)
    }

    /// Positions for the joining lines: warmer days sit higher, so the line reads
    /// as the shape of the week rather than as decoration.
    private func normalised(
        _ values: [Int], in size: CGSize, top: Bool
    ) -> [CGPoint] {
        guard !values.isEmpty else { return [] }
        let columnWidth = size.width / CGFloat(values.count)
        let low = values.min() ?? 0
        let high = values.max() ?? 1
        let span = max(1, high - low)
        // The bands the two lines live in, derived from where the column actually
        // lays out: high 26-42, icon 45-71, low 74-90, weekday 92-105 inside a
        // 132pt chart. The lows band was 88-104 and drew the line straight through
        // the weekday labels rather than through the low temperatures.
        let band: ClosedRange<CGFloat> = top ? 27...43 : 75...89
        return values.enumerated().map { index, value in
            let ratio = CGFloat(value - low) / CGFloat(span)
            let y = band.upperBound - ratio * (band.upperBound - band.lowerBound)
            return CGPoint(x: columnWidth * (CGFloat(index) + 0.5), y: y)
        }
    }

    private func polyline(through points: [CGPoint]) -> some View {
        Path { path in
            guard let first = points.first else { return }
            path.move(to: first)
            for point in points.dropFirst() { path.addLine(to: point) }
        }
        .stroke(.white.opacity(0.55), style: StrokeStyle(lineWidth: 2, lineCap: .round))
    }
}
