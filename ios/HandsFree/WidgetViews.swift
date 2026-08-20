import SwiftUI

/// Renders whatever widget the agent sent, over the live call surface.
///
/// This is the Phase 1 question in visual form: does a card appearing mid-call
/// read as help, or as an interruption? It is deliberately calm — slides up from
/// the bottom, no badge, no sound, dismissible with a swipe — because the agent
/// is interjecting into someone else's conversation and should behave like a
/// colleague sliding a note across the table rather than a notification.
struct WidgetRenderer: View {
    let envelope: WidgetEnvelope
    let onDismiss: () -> Void

    var body: some View {
        switch envelope.content {
        case let .flightResults(results):
            FlightResultsCard(results: results, onDismiss: onDismiss)
        case let .agentStatus(status):
            AgentStatusToast(status: status)
        }
    }
}

// MARK: - flight_results

struct FlightResultsCard: View {
    let results: FlightResults
    let onDismiss: () -> Void

    @State private var dragOffset: CGFloat = 0

    var body: some View {
        VStack(spacing: 0) {
            VStack(spacing: 0) {
                grabber
                header
                Divider().overlay(Color.white.opacity(0.08))

                VStack(spacing: 0) {
                    ForEach(Array(results.options.prefix(3).enumerated()), id: \.element.id) { index, option in
                        if index > 0 {
                            Divider()
                                .overlay(Color.white.opacity(0.06))
                                .padding(.leading, 18)
                        }
                        FlightRow(option: option)
                    }
                }
            }
            .background(
                // Slightly lifted off the call background rather than pure black,
                // so the card reads as a layer above the conversation.
                Color(white: 0.13),
                in: RoundedRectangle(cornerRadius: 22, style: .continuous)
            )
            .overlay(
                RoundedRectangle(cornerRadius: 22, style: .continuous)
                    .strokeBorder(Color.white.opacity(0.09), lineWidth: 0.5)
            )
            .padding(.horizontal, 10)
            .offset(y: max(0, dragOffset))
            .gesture(
                DragGesture()
                    .onChanged { dragOffset = $0.translation.height }
                    .onEnded { value in
                        // A deliberate downward flick dismisses; anything else
                        // springs back, so a stray touch never loses the card.
                        if value.translation.height > 80 || value.predictedEndTranslation.height > 160 {
                            onDismiss()
                        } else {
                            withAnimation(.spring(response: 0.3, dampingFraction: 0.8)) {
                                dragOffset = 0
                            }
                        }
                    }
            )
        }
    }

    private var grabber: some View {
        Capsule()
            .fill(Color.white.opacity(0.25))
            .frame(width: 34, height: 4)
            .padding(.top, 9)
            .padding(.bottom, 3)
    }

    private var header: some View {
        HStack(alignment: .center, spacing: 10) {
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 7) {
                    Text(results.route.origin)
                    Image(systemName: "arrow.right")
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(.white.opacity(0.4))
                    Text(results.route.destination)
                }
                .font(.system(size: 19, weight: .semibold, design: .rounded))
                .foregroundStyle(.white)

                Text(results.dateRangeText)
                    .font(.system(size: 12.5))
                    .foregroundStyle(.white.opacity(0.5))
            }

            Spacer()

            HStack(spacing: 5) {
                Image(systemName: "sparkles")
                    .font(.system(size: 10))
                Text("copilot")
                    .font(.system(size: 11, weight: .medium))
            }
            .foregroundStyle(.white.opacity(0.45))

            Button(action: onDismiss) {
                Image(systemName: "xmark")
                    .font(.system(size: 11, weight: .bold))
                    .foregroundStyle(.white.opacity(0.5))
                    .frame(width: 26, height: 26)
                    .background(Color.white.opacity(0.1), in: Circle())
            }
        }
        .padding(.horizontal, 18)
        .padding(.top, 11)
        .padding(.bottom, 13)
    }
}

private struct FlightRow: View {
    let option: FlightResults.Option

    var body: some View {
        Button {
            if let link = option.deeplink, let url = URL(string: link) {
                UIApplication.shared.open(url)
            }
        } label: {
            HStack(alignment: .center, spacing: 14) {
                VStack(alignment: .leading, spacing: 5) {
                    // Times first: it is the thing people compare.
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text(option.departTime)
                        Text("–")
                            .foregroundStyle(.white.opacity(0.35))
                        Text(option.arriveTime)
                        if option.crossesMidnight {
                            Text("+1")
                                .font(.system(size: 9.5, weight: .bold))
                                .foregroundStyle(.orange.opacity(0.9))
                                .offset(y: -4)
                        }
                    }
                    .font(.system(size: 17, weight: .medium, design: .rounded))
                    .foregroundStyle(.white)

                    HStack(spacing: 6) {
                        Text(option.carrier)
                        Text("·")
                        Text(option.durationText)
                        Text("·")
                        Text(option.stopsText)
                            .foregroundStyle(
                                option.stops == 0 ? .green.opacity(0.85) : .white.opacity(0.5)
                            )
                    }
                    .font(.system(size: 12))
                    .foregroundStyle(.white.opacity(0.5))
                    .lineLimit(1)
                }

                Spacer(minLength: 4)

                VStack(alignment: .trailing, spacing: 3) {
                    Text(option.priceText)
                        .font(.system(size: 16, weight: .semibold, design: .rounded))
                        .foregroundStyle(.white)
                    if option.deeplink != nil {
                        Text("Book")
                            .font(.system(size: 10.5, weight: .medium))
                            .foregroundStyle(.blue)
                    }
                }
            }
            .padding(.horizontal, 18)
            .padding(.vertical, 13)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        // allowsHitTesting rather than .disabled: a flight with no booking link
        // is still worth reading. .disabled() dims the entire row, including the
        // price, which throws away information to convey "not tappable".
        .allowsHitTesting(option.deeplink != nil)
    }
}

// MARK: - agent_status

struct AgentStatusToast: View {
    let status: AgentStatus

    var body: some View {
        HStack(spacing: 9) {
            if status.state == "error" {
                Image(systemName: "exclamationmark.triangle.fill")
                    .font(.system(size: 11))
                    .foregroundStyle(.orange)
            } else {
                ProgressView()
                    .controlSize(.mini)
                    .tint(.white.opacity(0.7))
            }
            Text(status.message)
                .font(.system(size: 13, weight: .medium))
                .foregroundStyle(.white.opacity(0.9))
        }
        .padding(.horizontal, 15)
        .padding(.vertical, 10)
        .background(.white.opacity(0.14), in: Capsule())
        .overlay(Capsule().strokeBorder(.white.opacity(0.1), lineWidth: 0.5))
        .padding(.top, 58)
    }
}
