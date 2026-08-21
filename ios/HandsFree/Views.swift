import Combine
import SwiftUI

// MARK: - Root

struct RootView: View {
    @EnvironmentObject private var app: AppState
    // Must observe CallCenter directly. Reading `app.callCenter.phase` looks
    // equivalent but is not: an ObservableObject does not republish a nested
    // ObservableObject's changes, so the call screen would never appear.
    @EnvironmentObject private var call: CallCenter
    // Same reason as CallCenter: a nested ObservableObject does not republish,
    // so the agent screen would never appear if this were read through `app`.
    @EnvironmentObject private var agent: AgentSession
    @EnvironmentObject private var flights: FlightStore
    @EnvironmentObject private var weather: WeatherStore

    var body: some View {
        ZStack {
            if app.isSignedIn {
                DialView()
            } else {
                LoginView()
            }
        }
        // The agent conversation owns the full screen while it is running.
        .fullScreenCover(isPresented: .constant(agent.isActive)) {
            AgentCallView()
                .environmentObject(app)
                .environmentObject(agent)
                .environmentObject(flights)
                .environmentObject(weather)
        }
        // The call surface covers everything. Widgets will render on top of this
        // in Phase 1, which is why the call view owns the full screen.
        .fullScreenCover(isPresented: .constant(isInCall)) {
            CallView()
                .environmentObject(app)
                .environmentObject(call)
        }
    }

    private var isInCall: Bool {
        switch call.phase {
        case .idle: return false
        default: return true
        }
    }
}

// MARK: - Login

struct LoginView: View {
    @EnvironmentObject private var app: AppState
    @State private var name = ""
    @State private var code = ""
    @State private var baseURL = Config.baseURL

    var body: some View {
        VStack(spacing: 0) {
            Spacer()

            VStack(spacing: 8) {
                Text("hands-free")
                    .font(.system(size: 34, weight: .semibold, design: .rounded))
                Text("Dev login — name and the code you were given")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
            }
            .padding(.bottom, 36)

            VStack(spacing: 12) {
                TextField("Your name", text: $name)
                    .textContentType(.givenName)
                    .autocorrectionDisabled()

                TextField("Code (e.g. KUSHAL-W2T)", text: $code)
                    .textInputAutocapitalization(.characters)
                    .autocorrectionDisabled()
                    .font(.system(.body, design: .monospaced))

                TextField("Server", text: $baseURL)
                    .keyboardType(.URL)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .font(.system(.footnote, design: .monospaced))
            }
            .textFieldStyle(.roundedBorder)
            .padding(.horizontal, 28)

            if let error = app.loginError {
                Text(error)
                    .font(.footnote)
                    .foregroundStyle(.red)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, 28)
                    .padding(.top, 12)
            }

            Button {
                Task { await app.logIn(code: code, displayName: name, baseURL: baseURL) }
            } label: {
                if app.isLoggingIn {
                    ProgressView().tint(.white).frame(maxWidth: .infinity)
                } else {
                    Text("Continue").frame(maxWidth: .infinity)
                }
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
            .disabled(name.isEmpty || code.isEmpty || app.isLoggingIn)
            .padding(.horizontal, 28)
            .padding(.top, 20)

            Spacer()
            Spacer()
        }
        .background(Color(.systemGroupedBackground))
    }
}

// MARK: - Contacts

struct ContactsView: View {
    @EnvironmentObject private var app: AppState

    var body: some View {
        NavigationStack {
            List {
                Section {
                    ForEach(app.contacts) { contact in
                        Button {
                            Task { await app.callCenter.startOutgoingCall(to: contact) }
                        } label: {
                            HStack(spacing: 14) {
                                Circle()
                                    .fill(contact.online ? Color.green : Color.secondary.opacity(0.3))
                                    .frame(width: 9, height: 9)
                                Text(contact.displayName)
                                    .foregroundStyle(.primary)
                                Spacer()
                                Image(systemName: "phone.fill")
                                    .foregroundStyle(.tint)
                            }
                        }
                    }
                } header: {
                    Text("People")
                } footer: {
                    Text(
                        "A green dot means their app is open and can ring. "
                            + "Background ringing arrives with PushKit later."
                    )
                }
            }
            .navigationTitle(app.session?.displayName ?? "hands-free")
            .refreshable { await app.refreshContacts() }
            .toolbar {
                ToolbarItem(placement: .navigationBarLeading) {
                    HStack(spacing: 5) {
                        Circle()
                            .fill(app.socketConnected ? Color.green : Color.orange)
                            .frame(width: 7, height: 7)
                        Text(app.socketConnected ? "live" : "offline")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                }
                ToolbarItem(placement: .navigationBarTrailing) {
                    Button("Sign out") { app.logOut() }
                        .font(.footnote)
                }
            }
            .task { await app.refreshContacts() }
        }
    }
}

// MARK: - Call

struct CallView: View {
    @EnvironmentObject private var app: AppState
    @EnvironmentObject private var call: CallCenter
    @State private var showDebug = false
    @State private var elapsed = 0

    /// Ticks the connected-state timer (68:2127). Driven by the view rather than
    /// by CallCenter so the call layer stays free of display concerns.
    private let tick = Timer.publish(every: 1, on: .main, in: .common).autoconnect()

    var body: some View {
        CallScaffold(
            gradient: Frost.callGradient,
            // The design shows one control in both states and it hangs up.
            buttonIcon: "pause.fill",
            onButton: { call.end() }
        ) {
            card
        }
        // Widgets and the agent indicator are laid over the designed surface
        // rather than folded into the card: the card's contents are fixed by the
        // design, and a flight card's height depends on how many flights came
        // back. Overlaying keeps both intact.
        .overlay(alignment: .top) { topOverlay }
        .overlay(alignment: .bottom) { widgetOverlay }
        .overlay(alignment: .topTrailing) { devTools }
        .overlay(alignment: .bottom) {
            if showDebug {
                DebugOverlay(lines: call.debugLines, quality: call.connectionQuality)
                    .transition(.move(edge: .bottom))
                    .zIndex(2)
            }
        }
        .onReceive(tick) { _ in
            if call.phase == .active || call.phase == .reconnecting { elapsed += 1 }
        }
        .animation(.snappy, value: call.agentPresent)
        .animation(.snappy, value: call.phase)
        .animation(.spring(response: 0.42, dampingFraction: 0.86), value: call.activeWidget?.widgetID)
    }

    // MARK: - The card

    private var isConnected: Bool {
        call.phase == .active || call.phase == .reconnecting
    }

    @ViewBuilder private var card: some View {
        if isConnected {
            connectedCard
        } else {
            callingCard
        }
    }

    /// Figma 68:2008 — ringing. One avatar at y=167, the meter under it, and the
    /// headline at y=390.
    private var callingCard: some View {
        FrostPanel {
            FrostAvatar(url: nil, name: call.remoteName)
                .offset(y: 167)

            // Bars span x 142-209 in a 356-wide card, so very slightly left of
            // centre — matched rather than rounded to centre.
            Waveform(
                color: Color(red: 209 / 255, green: 207 / 255, blue: 207 / 255), // #D1CFCF
                animating: true
            )
            .offset(x: (175.5 - Frost.cardSize.width / 2), y: 304 + 17.5 - 12.5)

            Text(headline)
                .font(Frost.headline)
                .foregroundStyle(Frost.headlineColor)
                .lineLimit(1)
                .minimumScaleFactor(0.6)
                .offset(y: 390)

            if let error = call.lastError {
                Text(error)
                    .font(.footnote)
                    .foregroundStyle(.red)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, 28)
                    .offset(y: 450)
            }
        }
    }

    /// Figma 68:2072 — connected. Two overlapping avatars, and only the remote
    /// one wears the ring.
    private var connectedCard: some View {
        FrostPanel {
            FrostAvatar(url: nil, name: app.session?.displayName ?? "You", ringed: false)
                .offset(x: -50.5, y: 118)

            FrostAvatar(url: nil, name: call.remoteName)
                .offset(x: 50.5, y: 149)

            // The meter sits under the right-hand avatar (x 193-260), not centred.
            Waveform(
                color: Color(red: 230 / 255, green: 230 / 255, blue: 230 / 255), // #E6E6E6
                animating: call.phase == .active
            )
            .offset(x: (226.5 - Frost.cardSize.width / 2), y: 304 - 0.5 - 12.5)

            Text(timerText)
                .font(Frost.headline)
                .foregroundStyle(Frost.headlineColor)
                .monospacedDigit()
                .offset(y: 391)

            if call.phase == .reconnecting {
                Text("Reconnecting…")
                    .font(.footnote.weight(.medium))
                    .foregroundStyle(Frost.nameColor)
                    .offset(y: 445)
            }
        }
    }

    private var headline: String {
        let name = call.remoteName.isEmpty ? "…" : call.remoteName
        switch call.phase {
        case .incomingRinging: return name
        case .connecting: return "Connecting"
        case let .ended(reason): return reason == "declined" ? "Declined" : "Call ended"
        default: return "Calling \(name)"
        }
    }

    private var timerText: String {
        String(format: "%02d:%02d", elapsed / 60, elapsed % 60)
    }

    // MARK: - Overlays (unchanged capability)

    @ViewBuilder private var topOverlay: some View {
        VStack(spacing: 8) {
            // Driven by participant metadata, not local state, so it always
            // reflects who is actually in the room. There is no consent gate.
            if call.agentPresent {
                HStack(spacing: 6) {
                    Image(systemName: "sparkles")
                    Text("Trip copilot is listening")
                }
                .font(.caption.weight(.medium))
                .foregroundStyle(Frost.nameColor)
                .padding(.horizontal, 12)
                .padding(.vertical, 7)
                .background(Frost.fill, in: Capsule())
                .overlay(Capsule().strokeBorder(Frost.border, lineWidth: 1))
                .transition(.move(edge: .top).combined(with: .opacity))
            }

            if let widget = call.activeWidget, case let .agentStatus(status) = widget.content {
                AgentStatusToast(status: status)
                    .transition(.move(edge: .top).combined(with: .opacity))
            }
        }
        .padding(.top, 8)
    }

    @ViewBuilder private var widgetOverlay: some View {
        if let widget = call.activeWidget, case .flightResults = widget.content {
            WidgetRenderer(envelope: widget) { call.dismissWidget() }
                .padding(.horizontal, 16)
                // Clear of the hang-up button rather than over it.
                .padding(.bottom, 190)
                .transition(.move(edge: .bottom).combined(with: .opacity))
        }
    }

    @ViewBuilder private var devTools: some View {
        // Dev builds only: the panel that answers "why didn't the agent fire?"
        // live on a call (spec 11.2), plus the widget-injection buttons.
        #if DEBUG
            HStack(spacing: 14) {
                Button { call.handleWidget(data: WidgetSamples.searching) } label: {
                    Image(systemName: "magnifyingglass")
                }
                Button { call.handleWidget(data: WidgetSamples.flightResults) } label: {
                    Image(systemName: "airplane")
                }
                Button { withAnimation(.snappy) { showDebug.toggle() } } label: {
                    Image(systemName: showDebug ? "ladybug.fill" : "ladybug")
                }
                if call.phase == .incomingRinging, !call.usesCallKit {
                    Button { Task { await call.answerFromApp() } } label: {
                        Image(systemName: "phone.fill").foregroundStyle(.green)
                    }
                }
            }
            .foregroundStyle(Frost.nameColor)
            .padding(20)
        #endif
    }
}

private struct CircleButton: View {
    let icon: String
    let tint: Color
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: icon)
                .font(.system(size: 24, weight: .medium))
                .foregroundStyle(.white)
                .frame(width: 66, height: 66)
                .background(tint, in: Circle())
        }
    }
}

// MARK: - Debug overlay

struct DebugOverlay: View {
    let lines: [String]
    let quality: String

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Spacer()
            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Text("DEBUG").font(.caption2.bold())
                    Spacer()
                    Text("quality: \(quality)").font(.caption2)
                }
                .foregroundStyle(.white.opacity(0.5))

                ScrollViewReader { proxy in
                    ScrollView {
                        VStack(alignment: .leading, spacing: 2) {
                            ForEach(Array(lines.enumerated()), id: \.offset) { index, line in
                                Text(line)
                                    .font(.system(size: 9.5, design: .monospaced))
                                    .foregroundStyle(.white.opacity(0.8))
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    .id(index)
                            }
                        }
                    }
                    .frame(height: 190)
                    // Single-parameter onChange: the two-parameter form is
                    // iOS 17+, and this app supports iOS 16.
                    .onChange(of: lines.count) { count in
                        withAnimation { proxy.scrollTo(count - 1, anchor: .bottom) }
                    }
                }
            }
            .padding(12)
            .background(.black.opacity(0.75))
        }
        .ignoresSafeArea(edges: .bottom)
    }
}
