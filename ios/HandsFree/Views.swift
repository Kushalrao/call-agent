import SwiftUI

// MARK: - Root

struct RootView: View {
    @EnvironmentObject private var app: AppState
    // Must observe CallCenter directly. Reading `app.callCenter.phase` looks
    // equivalent but is not: an ObservableObject does not republish a nested
    // ObservableObject's changes, so the call screen would never appear.
    @EnvironmentObject private var call: CallCenter

    var body: some View {
        ZStack {
            if app.isSignedIn {
                ContactsView()
            } else {
                LoginView()
            }
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

    var body: some View {
        ZStack {
            LinearGradient(
                colors: [Color(white: 0.09), Color(white: 0.16)],
                startPoint: .top,
                endPoint: .bottom
            )
            .ignoresSafeArea()

            VStack(spacing: 0) {
                // Agent indicator. Driven by participant metadata, not local
                // state, so it always reflects who is actually in the room. Not
                // dismissible and not a toggle — there is no consent gate.
                if call.agentPresent {
                    HStack(spacing: 6) {
                        Image(systemName: "sparkles")
                        Text("Trip copilot is listening")
                    }
                    .font(.caption.weight(.medium))
                    .foregroundStyle(.white.opacity(0.85))
                    .padding(.horizontal, 12)
                    .padding(.vertical, 7)
                    .background(.white.opacity(0.12), in: Capsule())
                    .padding(.top, 12)
                    .transition(.move(edge: .top).combined(with: .opacity))
                }

                Spacer()

                VStack(spacing: 10) {
                    Text(call.remoteName.isEmpty ? "…" : call.remoteName)
                        .font(.system(size: 32, weight: .semibold, design: .rounded))
                        .foregroundStyle(.white)

                    Text(statusText)
                        .font(.subheadline)
                        .foregroundStyle(.white.opacity(0.65))
                        .contentTransition(.opacity)

                    if call.phase == .reconnecting {
                        HStack(spacing: 6) {
                            ProgressView().controlSize(.mini).tint(.white)
                            Text("Reconnecting…").font(.caption)
                        }
                        .foregroundStyle(.white.opacity(0.8))
                        .padding(.horizontal, 12)
                        .padding(.vertical, 6)
                        .background(.orange.opacity(0.3), in: Capsule())
                    }
                }

                if let error = call.lastError {
                    Text(error)
                        .font(.caption)
                        .foregroundStyle(.red)
                        .multilineTextAlignment(.center)
                        .padding(.horizontal, 32)
                        .padding(.top, 16)
                }

                Spacer()

                // The card is laid out ABOVE the controls rather than overlaid
                // on top of them. Overlaying would mean guessing a padding value
                // that happens to clear a card whose height depends on how many
                // flights came back — and getting it wrong puts the agent
                // between two people and the end of their call.
                if let widget = call.activeWidget, case .flightResults = widget.content {
                    WidgetRenderer(envelope: widget) { call.dismissWidget() }
                        .transition(.move(edge: .bottom).combined(with: .opacity))
                        .padding(.bottom, 14)
                }

                controls
                    .padding(.bottom, 36)
            }

            if showDebug {
                DebugOverlay(lines: call.debugLines, quality: call.connectionQuality)
                    .transition(.move(edge: .bottom))
                    .zIndex(2)
            }
        }
        .overlay(alignment: .top) {
            if let widget = call.activeWidget, case let .agentStatus(status) = widget.content {
                AgentStatusToast(status: status)
                    .transition(.move(edge: .top).combined(with: .opacity))
            }
        }
        .overlay(alignment: .topTrailing) {
            // Dev builds only: the panel that answers "why didn't the agent
            // fire?" live on a call (spec 11.2), plus the Phase 1 spike buttons
            // that inject widget payloads with no agent involved.
            #if DEBUG
                HStack(spacing: 14) {
                    Button {
                        call.handleWidget(data: WidgetSamples.searching)
                    } label: {
                        Image(systemName: "magnifyingglass")
                            .foregroundStyle(.white.opacity(0.6))
                    }
                    Button {
                        call.handleWidget(data: WidgetSamples.flightResults)
                    } label: {
                        Image(systemName: "airplane")
                            .foregroundStyle(.white.opacity(0.6))
                    }
                    Button {
                        withAnimation(.snappy) { showDebug.toggle() }
                    } label: {
                        Image(systemName: showDebug ? "ladybug.fill" : "ladybug")
                            .foregroundStyle(.white.opacity(0.6))
                    }
                }
                .padding(20)
            #endif
        }
        .animation(.snappy, value: call.agentPresent)
        .animation(.snappy, value: call.phase)
        .animation(.spring(response: 0.42, dampingFraction: 0.86), value: call.activeWidget?.widgetID)
    }

    private var statusText: String {
        switch call.phase {
        case .idle: return ""
        case .outgoingRinging: return "Calling…"
        case .incomingRinging: return "Incoming call"
        case .connecting: return "Connecting…"
        case .active: return "Connected"
        case .reconnecting: return "Connected"
        case let .ended(reason): return reason == "declined" ? "Declined" : "Call ended"
        }
    }

    @ViewBuilder private var controls: some View {
        HStack(spacing: 28) {
            if call.phase == .incomingRinging {
                CircleButton(icon: "phone.down.fill", tint: .red) { call.end() }
                // On device the answer happens on CallKit's own call screen, so
                // this button is only the in-app path (Simulator).
                CircleButton(icon: "phone.fill", tint: .green) {
                    Task { await call.answerFromApp() }
                }
                .disabled(call.usesCallKit)
            } else {
                CircleButton(
                    icon: call.isMuted ? "mic.slash.fill" : "mic.fill",
                    tint: call.isMuted ? .white.opacity(0.35) : .white.opacity(0.18)
                ) { call.toggleMute() }

                CircleButton(icon: "phone.down.fill", tint: .red) { call.end() }

                CircleButton(
                    icon: call.isSpeaker ? "speaker.wave.2.fill" : "speaker.fill",
                    tint: call.isSpeaker ? .white.opacity(0.35) : .white.opacity(0.18)
                ) { call.toggleSpeaker() }
            }
        }
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
