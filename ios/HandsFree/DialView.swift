import SwiftUI

/// The idle screen — Figma `iPhone 14 Plus - 20`, node 139:2561.
///
/// Shared pieces live in CallSurface.swift; this file is only the idle state.
///
/// The design shows a person you are about to ring. What the button now starts is
/// a conversation with the copilot, so the card shows the copilot instead — the
/// layout, spacing and treatment are the design's; only the identity differs.
///
/// The design specifies Figtree, which is not bundled. The system font is used at
/// the same sizes and weights until the font files are added.
struct DialView: View {
    @EnvironmentObject private var app: AppState
    @EnvironmentObject private var agent: AgentSession

    /// Name sits 380 from the card's top edge (139:2612), not centred — the gap
    /// under the avatar is deliberately asymmetric.
    private let nameTopInset: CGFloat = 380
    private let liveDotSize: CGFloat = 9

    var body: some View {
        CallScaffold(
            gradient: Frost.dialGradient,
            buttonIcon: "phone.fill",
            // Tapping call starts a conversation with the copilot rather than
            // ringing another person.
            onButton: { Task { await agent.start() } }
        ) {
            card
        }
        .toolbar(.hidden, for: .navigationBar)
        .task { await app.refreshContacts() }
    }

    private var card: some View {
        FrostPanel {
            AgentAvatar()
                // Centred in the card, per 139:2609.
                .offset(y: (Frost.cardSize.height - Frost.avatarSize) / 2)

            nameBlock
                .offset(y: nameTopInset)
        }
    }

    private var nameBlock: some View {
        HStack(spacing: 7) {
            // Green only when the copilot can actually be started, which means
            // our server is reachable. A light that is always on means nothing;
            // the point of it is that tapping call will work.
            if app.socketConnected {
                Circle()
                    .fill(Color.green)
                    .frame(width: liveDotSize, height: liveDotSize)
                    .accessibilityLabel("Online")
            }
            Text("Trip Copilot")
                .font(Frost.name)
                .foregroundStyle(Frost.nameColor)
                .lineSpacing(23 - 17)
        }
    }
}
