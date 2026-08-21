import SwiftUI

/// The dial screen — Figma `iPhone 14 Plus - 20`, node 139:2561.
///
/// Shared pieces live in CallSurface.swift; this file is only what is specific to
/// the idle state.
///
/// Two deliberate departures:
///
/// - The design's avatar is a photograph of a specific person. That is placeholder
///   *content*, not a design asset, so it is not shipped — the real avatar comes
///   from `Contact.avatarUrl`, with initials when there is none.
/// - The design specifies Figtree, which is not bundled here. The system font is
///   used at the same sizes and weights until the font files are added.

/// Name sits 380 from the card's top edge (139:2612), not centred — the gap under
/// the avatar is deliberately asymmetric.
private let nameTopInset: CGFloat = 380
private let liveDotSize: CGFloat = 9

struct DialView: View {
    @EnvironmentObject private var app: AppState

    /// Whom the call button dials. The design shows a single person; when the
    /// account has more than one contact a compact selector appears under the
    /// name, so the one-contact case renders exactly as designed.
    @State private var selectedID: String?

    private var contacts: [Contact] { app.contacts }

    private var selected: Contact? {
        contacts.first { $0.id == selectedID } ?? contacts.first
    }

    var body: some View {
        CallScaffold(
            gradient: Frost.dialGradient,
            buttonIcon: "phone.fill",
            onButton: {
                guard let contact = selected else { return }
                Task { await app.callCenter.startOutgoingCall(to: contact) }
            }
        ) {
            card
        }
        .toolbar(.hidden, for: .navigationBar)
        .task { await app.refreshContacts() }
        .refreshable { await app.refreshContacts() }
    }

    // MARK: - Card

    private var card: some View {
        FrostPanel {
            FrostAvatar(url: selected?.avatarUrl, name: selected?.displayName ?? "")
                // Centred in the card, per 139:2609.
                .offset(y: (Frost.cardSize.height - Frost.avatarSize) / 2)

            nameBlock
                .offset(y: nameTopInset)
        }
    }

    private var nameBlock: some View {
        VStack(spacing: 12) {
            HStack(spacing: 7) {
                // Green only when they can actually be reached. Anything else
                // would be a light that means nothing — the whole point of it is
                // that ringing this person will work right now.
                if selected?.online == true {
                    Circle()
                        .fill(Color.green)
                        .frame(width: liveDotSize, height: liveDotSize)
                        .accessibilityLabel("Online")
                }
                Text(selected?.displayName ?? "No one to call")
                    .font(Frost.name)
                    .foregroundStyle(Frost.nameColor)
                    .lineSpacing(23 - 17)
            }

            if contacts.count > 1 {
                // Only when the account really has several people; with one
                // contact this renders nothing and the card matches the design.
                HStack(spacing: 10) {
                    ForEach(contacts) { contact in
                        Button {
                            selectedID = contact.id
                        } label: {
                            Text(contact.displayName)
                                .font(.system(size: 13, weight: .medium))
                                .foregroundStyle(
                                    contact.id == selected?.id
                                        ? Frost.nameColor : Frost.nameColor.opacity(0.45)
                                )
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
        }
    }
}

// Avatar rendering lives in CallSurface.swift (FrostAvatar), shared with the
// in-call screens. The design's photo is placeholder content standing in for
// whoever you are calling, not an asset to ship.
