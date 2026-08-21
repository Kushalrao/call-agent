import SwiftUI

/// Shared surface for the three call screens (Figma nodes 139:2561, 68:2008,
/// 68:2072). They are one design with three states, so the pieces live here
/// rather than being redrawn per screen — the frosted treatment is a border
/// colour and an opacity, and three copies of that drift apart.
enum Frost {
    static let cardSize = CGSize(width: 356, height: 608)
    static let cardRadius: CGFloat = 44
    static let cardCenterOffsetY: CGFloat = -50

    static let fill = Color.white.opacity(0.6)
    static let border = Color(red: 245 / 255, green: 245 / 255, blue: 233 / 255) // #F5F5E9
    static let borderWidth: CGFloat = 2

    static let buttonSize = CGSize(width: 88, height: 90)
    static let buttonRadius: CGFloat = 33
    /// 693 on the design's 844pt frame, scaled per device.
    static let buttonTop: CGFloat = 693
    static let designFrameHeight: CGFloat = 844

    static let avatarSize: CGFloat = 123
    static let avatarRadius: CGFloat = 47
    static let avatarRingWidth: CGFloat = 9

    /// Big label: Figtree ExtraBold 37 (68:2067, 68:2127).
    static let headlineColor = Color(red: 39 / 255, green: 42 / 255, blue: 36 / 255).opacity(0.9)
    static let headline = Font.system(size: 37, weight: .heavy)
    /// Name on the dial screen: Figtree SemiBold 17 (139:2612).
    static let nameColor = Color(red: 46 / 255, green: 49 / 255, blue: 44 / 255).opacity(0.67)
    static let name = Font.system(size: 17, weight: .semibold)

    /// Dial screen background (139:2561).
    static let dialGradient: [Gradient.Stop] = [
        .init(color: Color(red: 245 / 255, green: 245 / 255, blue: 237 / 255), location: 0),
        .init(color: Color(red: 219 / 255, green: 219 / 255, blue: 208 / 255), location: 0.338),
        .init(color: Color(red: 229 / 255, green: 227 / 255, blue: 190 / 255), location: 0.75439),
        .init(color: Color(red: 237 / 255, green: 255 / 255, blue: 242 / 255), location: 1),
    ]

    /// In-call background (68:2008, 68:2072) — cooler at the top, pink at the
    /// bottom rather than green. A different palette, not a reused one.
    static let callGradient: [Gradient.Stop] = [
        .init(color: Color(red: 237 / 255, green: 245 / 255, blue: 244 / 255), location: 0),
        .init(color: Color(red: 210 / 255, green: 208 / 255, blue: 219 / 255), location: 0.338),
        .init(color: Color(red: 229 / 255, green: 223 / 255, blue: 190 / 255), location: 0.75439),
        .init(color: Color(red: 255 / 255, green: 237 / 255, blue: 238 / 255), location: 1),
    ]
}

/// The frosted panel.
///
/// Note there is no material behind the white fill. The design specifies a
/// 67.5px backdrop blur, but everything behind this card is a smooth vertical
/// gradient — and blurring a smooth gradient is visually a no-op. Adding
/// `.ultraThinMaterial` for the sake of matching the property name made the card
/// visibly grey against a design that is barely-there warm white, so the fill is
/// what actually reproduces it.
struct FrostPanel<Content: View>: View {
    var size: CGSize = Frost.cardSize
    var radius: CGFloat = Frost.cardRadius
    @ViewBuilder var content: Content

    var body: some View {
        ZStack(alignment: .top) {
            // Establishes the card's full size *inside* the stack. Without it the
            // ZStack sizes to its content, so `.top` aligns to that content box
            // rather than to the card — every offset in the designs is measured
            // from the card's top edge, and they all landed hundreds of points
            // low, with the headline clipped away entirely.
            Color.clear
                .frame(width: size.width, height: size.height)
            content
        }
        .frame(width: size.width, height: size.height, alignment: .top)
            .background(
                RoundedRectangle(cornerRadius: radius, style: .continuous)
                    .fill(Frost.fill)
            )
            .overlay(
                RoundedRectangle(cornerRadius: radius, style: .continuous)
                    .strokeBorder(Frost.border, lineWidth: Frost.borderWidth)
            )
            .clipShape(RoundedRectangle(cornerRadius: radius, style: .continuous))
    }
}

/// The six-bar level meter (68:2060-68:2065, 68:2120-68:2125).
///
/// Heights and pitch are the designed ones. It animates while the call is live
/// because a static level meter reads as a broken one, and rests at the designed
/// silhouette otherwise.
struct Waveform: View {
    /// #D1CFCF while calling, #E6E6E6 once connected — the design dims it after
    /// the call is answered.
    let color: Color
    var animating: Bool = false

    private static let heights: [CGFloat] = [25, 25, 13, 21, 25, 5]
    private static let barWidth: CGFloat = 7
    private static let pitch: CGFloat = 12

    @State private var phase = 0.0

    var body: some View {
        HStack(spacing: Self.pitch - Self.barWidth) {
            ForEach(Array(Self.heights.enumerated()), id: \.offset) { index, height in
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .fill(color)
                    .frame(width: Self.barWidth, height: scaled(height, index))
            }
        }
        .frame(height: Self.heights.max() ?? 25)
        .onAppear {
            guard animating else { return }
            withAnimation(.easeInOut(duration: 0.7).repeatForever(autoreverses: true)) {
                phase = 1
            }
        }
        .animation(.easeInOut(duration: 0.7), value: animating)
    }

    private func scaled(_ height: CGFloat, _ index: Int) -> CGFloat {
        guard animating else { return height }
        // Alternating so neighbours move against each other; a uniform pulse
        // looks like one object breathing rather than a level meter.
        let swing: CGFloat = index.isMultiple(of: 2) ? 0.45 : -0.35
        return max(5, height * (1 + swing * phase))
    }
}

/// The single frosted control. In both call states this ends the call.
struct FrostButton: View {
    let icon: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: icon)
                .font(.system(size: 30, weight: .regular))
                .foregroundStyle(.black)
                .frame(width: Frost.buttonSize.width, height: Frost.buttonSize.height)
                .background(
                    RoundedRectangle(cornerRadius: Frost.buttonRadius, style: .continuous)
                        .fill(Frost.fill)
                )
                .overlay(
                    RoundedRectangle(cornerRadius: Frost.buttonRadius, style: .continuous)
                        .strokeBorder(Frost.border, lineWidth: Frost.borderWidth)
                )
        }
        .buttonStyle(.plain)
    }
}

/// A 123pt avatar with the designed radius, optionally wearing the 9pt white
/// ring. On the connected screen only the remote participant has the ring
/// (68:2118); the local one does not (68:2116).
struct FrostAvatar: View {
    let url: String?
    let name: String
    var ringed: Bool = true

    var body: some View {
        content
            .frame(width: Frost.avatarSize, height: Frost.avatarSize)
            .clipShape(RoundedRectangle(cornerRadius: Frost.avatarRadius, style: .continuous))
            .overlay {
                if ringed {
                    RoundedRectangle(cornerRadius: Frost.avatarRadius, style: .continuous)
                        .strokeBorder(.white, lineWidth: Frost.avatarRingWidth)
                }
            }
    }

    @ViewBuilder
    private var content: some View {
        if let url, let parsed = URL(string: url) {
            AsyncImage(url: parsed) { phase in
                if case let .success(image) = phase {
                    // The design overflows the photo and clips it, so fill —
                    // fit would letterbox and show background inside the ring.
                    image.resizable().scaledToFill()
                } else {
                    initials
                }
            }
        } else {
            initials
        }
    }

    private var initials: some View {
        ZStack {
            Color(red: 232 / 255, green: 232 / 255, blue: 222 / 255)
            Text(monogram)
                .font(.system(size: 42, weight: .semibold))
                .foregroundStyle(Frost.nameColor)
        }
    }

    private var monogram: String {
        let letters = name.split(separator: " ").prefix(2).compactMap(\.first).map(String.init)
        return letters.isEmpty ? "?" : letters.joined().uppercased()
    }
}

/// Positions the card and the button the way all three screens do.
struct CallScaffold<Card: View>: View {
    let gradient: [Gradient.Stop]
    let buttonIcon: String
    let onButton: () -> Void
    @ViewBuilder var card: Card

    var body: some View {
        ZStack {
            LinearGradient(stops: gradient, startPoint: .top, endPoint: .bottom)
                .ignoresSafeArea()

            GeometryReader { geo in
                ZStack {
                    card.position(
                        x: geo.size.width / 2,
                        y: geo.size.height / 2 + Frost.cardCenterOffsetY
                    )
                    FrostButton(icon: buttonIcon, action: onButton)
                        .position(
                            x: geo.size.width / 2,
                            y: (Frost.buttonTop + Frost.buttonSize.height / 2)
                                / Frost.designFrameHeight * geo.size.height
                        )
                }
            }
        }
    }
}
