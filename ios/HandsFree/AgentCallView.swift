import SwiftUI

/// The screen while you are talking to the agent.
///
/// Reuses the in-call design (Figma 68:2008 / 68:2072) rather than inventing a
/// third look: it is still a voice call, just with one human in it. The left
/// avatar is you and the right one is the agent, which is what the two-avatar
/// connected state was already shaped for.
struct AgentCallView: View {
    @EnvironmentObject private var app: AppState
    @EnvironmentObject private var agent: AgentSession
    @EnvironmentObject private var flights: FlightStore

    var body: some View {
        CallScaffold(
            gradient: Frost.callGradient,
            buttonIcon: "pause.fill",
            onButton: { Task { await agent.stop() } }
        ) {
            FrostPanel {
                if flights.hasResults {
                    // Results replace the avatars rather than sitting under them:
                    // the card is a fixed size from the design, and twelve rows
                    // plus two 123pt avatars do not fit in it.
                    resultsCard
                } else {
                    waitingCard
                }
            }
        }
        .animation(.snappy, value: agent.phase)
        .animation(.snappy, value: agent.agentAudible)
        .animation(.spring(response: 0.4, dampingFraction: 0.85), value: flights.visible.count)
        .task { flights.start() }
        // The agent asks for a shape; the store owns the rows. Kept as a one-way
        // sync so there is a single place that decides what is on screen.
        // Single-parameter onChange: the two-parameter form is iOS 17+, and this
        // app ships to 16 for the iPhone X.
        .onChange(of: agent.filter) { newFilter in flights.filter = newFilter }
        .onDisappear { flights.stop() }
    }

    // MARK: - Results (Figma 171:2813)

    private var resultsCard: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 8) {
                Text(flights.destination)
                    .font(.system(size: 17, weight: .semibold))
                    .foregroundStyle(Frost.nameColor)
                if let narrowing = flights.filter.label {
                    Text(narrowing)
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(Frost.nameColor.opacity(0.7))
                        .padding(.horizontal, 8)
                        .padding(.vertical, 3)
                        .background(Color.white.opacity(0.55), in: Capsule())
                }
                Spacer()
                Text(timerText)
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(Frost.nameColor)
                    .monospacedDigit()
            }
            .padding(.horizontal, 20)
            .padding(.top, 22)
            .padding(.bottom, 4)

            FlightList(flights: flights.visible)
        }
    }

    private var timerText: String {
        String(format: "%02d:%02d", agent.elapsed / 60, agent.elapsed % 60)
    }

    private var waitingCard: some View {
        ZStack(alignment: .top) {
            Color.clear.frame(width: Frost.cardSize.width, height: Frost.cardSize.height)

            FrostAvatar(
                    url: nil,
                    name: app.session?.displayName ?? "You",
                    ringed: false
                )
                .offset(x: -50.5, y: 118)

                AgentAvatar()
                    .offset(x: 50.5, y: 149)

                // Only moves once the agent's audio track is actually
                // subscribed. A meter that animates before playback exists is
                // how "it looked like it was talking" becomes a whole day of
                // debugging silence.
                Waveform(
                    color: Color(red: 230 / 255, green: 230 / 255, blue: 230 / 255),
                    animating: agent.agentAudible
                )
                .offset(x: (226.5 - Frost.cardSize.width / 2), y: 304 - 0.5 - 12.5)

                Text(headline)
                    .font(Frost.headline)
                    .foregroundStyle(Frost.headlineColor)
                    .monospacedDigit()
                    .lineLimit(1)
                    .minimumScaleFactor(0.6)
                    .offset(y: 391)

                if let error = agent.lastError {
                    Text(error)
                        .font(.footnote)
                        .foregroundStyle(.red)
                        .multilineTextAlignment(.center)
                        .padding(.horizontal, 28)
                        .offset(y: 450)
                } else if let status = subtitle {
                    Text(status)
                        .font(.footnote.weight(.medium))
                        .foregroundStyle(Frost.nameColor)
                        .offset(y: 450)
            }
        }
    }

    // These used to both read "Connecting", which made the two states
    // indistinguishable on the phone — "still dialling" and "connected, the
    // agent has not arrived" have completely different causes and the screen was
    // hiding which one you were in.
    private var headline: String {
        switch agent.phase {
        case .idle: return ""
        case .requestingToken: return "Starting"
        case .connecting: return "Connecting"
        case .waitingForAgent: return "Joining"
        case .live:
            return String(format: "%02d:%02d", agent.elapsed / 60, agent.elapsed % 60)
        case let .ended(reason):
            return reason == "failed" ? "Couldn't connect" : "Ended"
        }
    }

    private var subtitle: String? {
        switch agent.phase {
        case .requestingToken: return "Starting the copilot…"
        case .connecting: return "Reaching the copilot…"
        case .waitingForAgent: return "In the room — waiting for the copilot…"
        case .live: return agent.agentAudible ? nil : "Copilot has no voice yet…"
        default: return nil
        }
    }
}
