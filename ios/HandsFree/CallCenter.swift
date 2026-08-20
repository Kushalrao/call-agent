import AVFAudio
import CallKit
import Combine
import Foundation
import LiveKit

enum CallPhase: Equatable {
    case idle
    case outgoingRinging
    case incomingRinging
    case connecting
    case active
    case reconnecting
    case ended(reason: String)
}

/// CallKit + LiveKit, in one place.
///
/// Why CallKit at all, when we could just show our own UI: it gives us the
/// system call screen, correct audio routing, and — most importantly — correct
/// behaviour when a real cellular call interrupts ours. Rolling our own would
/// get the happy path right and the interruption cases wrong.
@MainActor
final class CallCenter: NSObject, ObservableObject {
    @Published private(set) var phase: CallPhase = .idle
    @Published private(set) var remoteName: String = ""
    @Published private(set) var isMuted = false
    @Published private(set) var isSpeaker = true
    @Published private(set) var agentPresent = false
    @Published private(set) var connectionQuality: String = "—"
    @Published private(set) var lastError: String?

    /// Debug overlay feed (spec 11.2), dev builds only.
    @Published private(set) var debugLines: [String] = []

    /// The widget currently on screen, if any (spec section 7). One at a time:
    /// a stack of cards over a live conversation would be noise, so a newer
    /// widget replaces the older one.
    @Published private(set) var activeWidget: WidgetEnvelope?

    private var widgetExpiry: Task<Void, Never>?

    private let provider: CXProvider
    private let controller = CXCallController()
    private var room: Room?

    /// The CallKit UUID and our server-side call id are different namespaces;
    /// every delegate callback arrives with the former and needs the latter.
    private var callKitUUID: UUID?
    private var callId: String?
    private var pendingToken: String?
    private var pendingLivekitURL: String?
    private var isCallee = false

    /// Set once we've told the server we joined, so a reconnect doesn't double-report.
    private var reportedJoined = false

    /// Whether to drive the call through CallKit.
    ///
    /// CallKit is not dependable on the Simulator: `providerDidReset` fires
    /// spontaneously right after `reportNewIncomingCall`, which would kill a
    /// perfectly good call. (PushKit doesn't work there either, for the same
    /// underlying reason.) So the Simulator gets in-app ringing and lets LiveKit
    /// own AVAudioSession, while devices get the real CallKit path. Everything
    /// downstream of "answer" - token fetch, room connect, teardown - is shared,
    /// so the Simulator still exercises the code that matters.
    /// Whether to route calls through CallKit at all.
    ///
    /// Not a compile-time constant, because whether CallKit *works* is a
    /// property of the build's entitlements rather than of the platform.
    /// `CXStartCallAction` fails with `CXErrorCodeRequestTransactionError`
    /// `.unentitled` (code 1) unless the app declares the `voip` background
    /// mode — and declaring `voip` without a provisioning profile that grants it
    /// makes iOS 26+ terminate the app at launch. On a free Apple ID those are
    /// mutually exclusive, so the honest answer is to check and adapt.
    ///
    /// Reading the plist rather than catching the error means we never attempt a
    /// transaction we know will be refused, and the fallback is chosen before
    /// the user taps anything instead of after a visible failure.
    ///
    /// The in-app path below is not a degraded stub: it already exists for the
    /// Simulator, and everything that matters downstream — token fetch, room
    /// connect, audio, the agent — is shared. What is lost is the system call UI.
    /// When a paid team's profile adds `voip` back to Info.plist, CallKit lights
    /// up again with no code change.
    static let usesCallKit: Bool = {
        #if targetEnvironment(simulator)
            // CallKit is unreliable here: providerDidReset fires immediately
            // after reportNewIncomingCall and kills a perfectly good call.
            return false
        #else
            let modes = Bundle.main.object(forInfoDictionaryKey: "UIBackgroundModes")
                as? [String] ?? []
            return modes.contains("voip")
        #endif
    }()

    var usesCallKit: Bool { Self.usesCallKit }

    override init() {
        let config = CXProviderConfiguration()
        config.supportsVideo = false
        config.maximumCallGroups = 1
        config.maximumCallsPerCallGroup = 1
        config.supportedHandleTypes = [.generic]
        provider = CXProvider(configuration: config)
        super.init()
        provider.setDelegate(self, queue: nil)

        // Hand AVAudioSession ownership to CallKit.
        //
        // LiveKit configures and activates the session itself by default. Under
        // CallKit that is wrong twice over: the system owns activation (an app
        // that calls setActive(true) itself fights it and gets one-way or dead
        // audio), and the category must be in place before the system activates.
        // So: disable automatic configuration, and keep the audio engine from
        // starting until CallKit hands us an activated session.
        // Deliberately NOT configuring audio here. This initialiser runs during
        // app startup, and setEngineAvailability reaches into WebRTC's audio
        // device module - initialising the audio stack before the UI exists is
        // both unnecessary and a plausible way to get killed at launch.
        // prepareAudioStack() does it once, when a call actually starts.
    }

    private var audioStackPrepared = false

    /// Hand AVAudioSession ownership to CallKit. Called on the first call, not
    /// at launch.
    ///
    /// LiveKit configures and activates the session itself by default. Under
    /// CallKit that is wrong twice over: the system owns activation (an app that
    /// calls setActive(true) itself fights it and gets one-way or dead audio),
    /// and the category must be in place before the system activates. Without
    /// CallKit, LiveKit's own management is correct - leave it alone rather than
    /// half-configuring it.
    private func prepareAudioStack() {
        guard usesCallKit, !audioStackPrepared else { return }
        audioStackPrepared = true
        AudioManager.shared.audioSession.isAutomaticConfigurationEnabled = false
        _ = try? AudioManager.shared.setEngineAvailability(.none)
        log("audio stack handed to CallKit")
    }

    // MARK: - Outgoing

    func startOutgoingCall(to contact: Contact) async {
        guard phase == .idle else { return }
        prepareAudioStack()
        remoteName = contact.displayName
        phase = .outgoingRinging
        log("placing call to \(contact.displayName)")

        do {
            let call = try await APIClient.shared.createCall(calleeId: contact.id)
            callId = call.callId
            pendingToken = call.lkToken
            pendingLivekitURL = call.livekitUrl
            isCallee = false

            let uuid = UUID()
            callKitUUID = uuid

            // Route the outgoing call through CallKit too, so system UI,
            // interruption handling, and audio routing all behave.
            guard usesCallKit else {
                // In-app path: nothing to do until the callee answers.
                return
            }
            let handle = CXHandle(type: .generic, value: contact.displayName)
            let action = CXStartCallAction(call: uuid, handle: handle)
            try await controller.requestTransaction(with: action)
        } catch let error as NSError
            where error.domain == CXErrorDomainRequestTransaction && error.code == 1 {
            // .unentitled — the voip background mode is declared but the
            // provisioning profile does not actually grant it. Nothing the user
            // can do, so say what it is rather than leaking an NSError.
            log("CallKit unentitled; profile lacks the VoIP capability")
            fail("This build can't use the system call UI. Re-sign with a team that has VoIP enabled, or remove the voip background mode.")
        } catch {
            fail("Couldn't start the call: \(error.localizedDescription)")
        }
    }

    /// The caller connects as soon as the callee answers — signalled over the
    /// ringing socket, which the caller is also listening on.
    func calleeAccepted(callId: String) async {
        guard callId == self.callId else { return }
        log("callee accepted")
        phase = .connecting
        await connectToRoom()
    }

    // MARK: - Incoming

    /// Report an incoming call to CallKit.
    ///
    /// In Phase 7 this is called from `pushRegistry(_:didReceiveIncomingPushWith:)`
    /// where it MUST run before that method returns — failing to report a VoIP
    /// push synchronously gets the app terminated and eventually loses the push
    /// entitlement. Keeping the reporting logic here, callable from either
    /// transport, is what makes that swap safe.
    func reportIncomingCall(callId: String, callerName: String) {
        prepareAudioStack()
        guard phase == .idle else {
            log("busy — ignoring incoming call \(callId.prefix(8))")
            return
        }
        let uuid = UUID()
        callKitUUID = uuid
        self.callId = callId
        remoteName = callerName
        isCallee = true
        phase = .incomingRinging

        guard usesCallKit else {
            log("incoming call from \(callerName) (in-app ringing)")
            return
        }

        let update = CXCallUpdate()
        update.remoteHandle = CXHandle(type: .generic, value: callerName)
        update.hasVideo = false
        update.localizedCallerName = callerName

        provider.reportNewIncomingCall(with: uuid, update: update) { [weak self] error in
            if let error {
                Task { @MainActor in
                    self?.fail("Couldn't show the incoming call: \(error.localizedDescription)")
                }
            }
        }
        log("incoming call from \(callerName)")
    }

    // MARK: - User actions

    /// Answer from in-app UI (Simulator). On device this happens through
    /// CallKit's own call screen and `CXAnswerCallAction` instead — both land in
    /// the same `acceptAndConnect()`.
    func answerFromApp() async {
        guard !usesCallKit, phase == .incomingRinging else { return }
        await acceptAndConnect()
    }

    /// Fetch a token for an accepted call and join the room.
    private func acceptAndConnect() async {
        guard let callId else { return }
        phase = .connecting
        do {
            let call = try await APIClient.shared.accept(callId: callId)
            pendingToken = call.lkToken
            pendingLivekitURL = call.livekitUrl
            await connectToRoom()
        } catch {
            fail("Couldn't answer: \(error.localizedDescription)")
        }
    }

    func end() {
        guard usesCallKit, let uuid = callKitUUID else {
            let wasRinging = phase == .incomingRinging
            let id = callId
            Task {
                if wasRinging, let id {
                    _ = try? await APIClient.shared.decline(callId: id)
                    await teardown(reason: "declined", notifyServer: false)
                } else {
                    await teardown(reason: "ended")
                }
            }
            return
        }
        let action = CXEndCallAction(call: uuid)
        controller.requestTransaction(with: action) { [weak self] error in
            if error != nil {
                // CallKit refused (already ended). Tear down anyway so the UI
                // can't get stuck in a call that no longer exists.
                Task { @MainActor in await self?.teardown(reason: "ended") }
            }
        }
    }

    func toggleMute() {
        isMuted.toggle()
        let muted = isMuted
        Task {
            try? await room?.localParticipant.setMicrophone(enabled: !muted)
            log(muted ? "muted" : "unmuted")
        }
    }

    func toggleSpeaker() {
        isSpeaker.toggle()
        // Speaker default is deliberate: people will be looking at widgets on
        // screen, not holding the phone to their ear (spec 2.2).
        AudioManager.shared.audioSession.isSpeakerOutputPreferred = isSpeaker
        log(isSpeaker ? "speaker on" : "speaker off")
    }

    // MARK: - Widgets

    /// Handle one widget payload from the agent.
    ///
    /// Unknown `type` is dropped and logged, never surfaced as an error: a newer
    /// agent must be able to ship a widget this build has never heard of (spec
    /// section 7 forward compatibility).
    func handleWidget(data: Data) {
        do {
            let envelope = try WidgetEnvelope.decode(data)
            show(envelope)
        } catch let WidgetDecodeError.unknownType(type) {
            log("widget dropped, unknown type: \(type)")
        } catch let WidgetDecodeError.unsupportedVersion(v) {
            log("widget dropped, unsupported envelope version: \(v)")
        } catch {
            log("widget decode failed: \(error)")
        }
    }

    func show(_ envelope: WidgetEnvelope) {
        activeWidget = envelope
        log("widget rendered: \(envelope.type) (ttl \(envelope.ttlSeconds)s)")

        // Widgets expire on their own. A stale flight price left on screen for
        // the rest of the call is worse than no card at all.
        widgetExpiry?.cancel()
        let ttl = envelope.ttlSeconds
        let id = envelope.widgetID
        widgetExpiry = Task { [weak self] in
            try? await Task.sleep(for: .seconds(ttl))
            guard !Task.isCancelled else { return }
            await MainActor.run {
                guard let self, self.activeWidget?.widgetID == id else { return }
                self.activeWidget = nil
                self.log("widget expired: \(id.prefix(8))")
            }
        }
    }

    /// Phase 1 spike hook: launch with `-auto_widget 1` and a sample card
    /// appears shortly after the call goes active. Same code path as the debug
    /// airplane button, driven from a launch argument so it can be scripted.
    private func maybeShowSampleWidget() {
        #if DEBUG
            guard UserDefaults.standard.bool(forKey: "auto_widget") else { return }
            Task { [weak self] in
                try? await Task.sleep(for: .seconds(2))
                self?.handleWidget(data: WidgetSamples.flightResults)
            }
        #endif
    }

    func dismissWidget() {
        guard let current = activeWidget else { return }
        widgetExpiry?.cancel()
        activeWidget = nil
        // Dismissals are the signal for whether proactive triggers are welcome
        // or annoying — worth logging from day one so the rate is measurable.
        log("widget dismissed by user: \(current.type)")
    }

    // MARK: - Room

    private func connectToRoom() async {
        guard let token = pendingToken, let urlString = pendingLivekitURL, let callId else {
            fail("Missing connection details.")
            return
        }

        let room = Room()
        room.add(delegate: self)
        self.room = room

        do {
            try await room.connect(
                url: urlString,
                token: token,
                connectOptions: ConnectOptions(autoSubscribe: true)
            )
            // With CallKit off, nothing else configures the session: the two
            // call sites for configureAudioSession() are both CXProvider
            // delegate methods, and those never fire on the in-app path. LiveKit
            // would auto-configure, but that leaves the category and options to
            // it — and `.defaultToSpeaker` matters here, because people are
            // looking at the screen rather than holding the phone to an ear.
            if !usesCallKit { configureAudioSession() }

            // Publish the mic only after connecting, so a failed connect never
            // leaves a hot mic with nowhere to send audio.
            try await room.localParticipant.setMicrophone(enabled: !isMuted)
            log("room connected, mic published")

            if !reportedJoined {
                reportedJoined = true
                _ = try? await APIClient.shared.reportJoined(callId: callId)
            }
            phase = .active
            maybeShowSampleWidget()
        } catch {
            // A 120s join window can lapse if the user sat on the ringing
            // screen. One retry with a fresh token before giving up.
            log("connect failed, refreshing token: \(error.localizedDescription)")
            if let refreshed = try? await APIClient.shared.refreshToken(callId: callId),
               let newToken = refreshed.lkToken
            {
                pendingToken = newToken
                do {
                    try await room.connect(
                        url: urlString,
                        token: newToken,
                        connectOptions: ConnectOptions(autoSubscribe: true)
                    )
                    try await room.localParticipant.setMicrophone(enabled: !isMuted)
                    if !reportedJoined {
                        reportedJoined = true
                        _ = try? await APIClient.shared.reportJoined(callId: callId)
                    }
                    phase = .active
                    return
                } catch {
                    fail("Couldn't join the call: \(error.localizedDescription)")
                    return
                }
            }
            fail("Couldn't join the call: \(error.localizedDescription)")
        }
    }

    private func hangUpServerSide() async {
        guard let callId else { return }
        _ = try? await APIClient.shared.end(callId: callId)
    }

    /// Server told us the call is over (the other party ended, declined, or it
    /// timed out). Tell CallKit so the system UI clears.
    func remoteEnded(callId: String, reason: String) {
        guard callId == self.callId else { return }
        if let uuid = callKitUUID {
            let cxReason: CXCallEndedReason =
                reason == "declined" ? .unanswered
                    : reason == "timeout" ? .unanswered : .remoteEnded
            provider.reportCall(with: uuid, endedAt: nil, reason: cxReason)
        }
        Task { await teardown(reason: reason, notifyServer: false) }
    }

    private func teardown(reason: String, notifyServer: Bool = true) async {
        log("teardown: \(reason)")
        if notifyServer { await hangUpServerSide() }

        if let room {
            await room.disconnect()
            room.remove(delegate: self)
        }
        room = nil
        widgetExpiry?.cancel()
        activeWidget = nil
        if usesCallKit { _ = try? AudioManager.shared.setEngineAvailability(.none) }

        callKitUUID = nil
        callId = nil
        pendingToken = nil
        pendingLivekitURL = nil
        reportedJoined = false
        agentPresent = false
        isMuted = false
        connectionQuality = "—"
        phase = .ended(reason: reason)

        // Brief terminal state so the UI can show "call ended", then idle.
        try? await Task.sleep(for: .milliseconds(1200))
        if case .ended = phase { phase = .idle }
    }

    private func fail(_ message: String) {
        lastError = message
        log("ERROR \(message)")
        Task { await teardown(reason: "error") }
    }

    func log(_ line: String) {
        let stamp = Date().formatted(date: .omitted, time: .standard)
        debugLines.append("\(stamp)  \(line)")
        if debugLines.count > 60 { debugLines.removeFirst(debugLines.count - 60) }
        print("[call] \(line)")
    }

    /// Configure the category, never activate. CallKit owns activation.
    private func configureAudioSession() {
        let session = AVAudioSession.sharedInstance()

        // `allowBluetooth` was renamed to `allowBluetoothHFP` in the iOS 26 SDK.
        // Both must be kept: this app ships to iOS 16 (iPhone X tops out at
        // 16.7) and runs on iOS 27, and the old name is deprecated rather than
        // removed, so the runtime behaviour is identical either way.
        var options: AVAudioSession.CategoryOptions = [.defaultToSpeaker]
        if #available(iOS 26.0, *) {
            options.insert(.allowBluetoothHFP)
        } else {
            options.insert(.allowBluetooth)
        }

        do {
            try session.setCategory(.playAndRecord, mode: .voiceChat, options: options)
        } catch {
            log("audio session category failed: \(error.localizedDescription)")
        }
    }
}

// MARK: - CXProviderDelegate

extension CallCenter: CXProviderDelegate {
    nonisolated func providerDidReset(_: CXProvider) {
        Task { @MainActor in
            // A reset fires at launch when the system has no calls, which is
            // normal. Only tear down if we actually have a call in flight -
            // otherwise the launch-time reset would `/end` nothing, or worse,
            // a stale id.
            guard callId != nil else {
                log("CallKit provider reset (no active call)")
                return
            }
            log("CallKit provider reset — dropping call")
            await teardown(reason: "reset")
        }
    }

    /// Outgoing call: CallKit has accepted the start action.
    nonisolated func provider(_: CXProvider, perform action: CXStartCallAction) {
        Task { @MainActor in
            configureAudioSession()
            provider.reportOutgoingCall(with: action.callUUID, startedConnectingAt: nil)
            action.fulfill()
            // Do not connect yet — wait for the callee to answer, which arrives
            // over the ringing socket as `call_accepted`.
        }
    }

    /// Incoming call answered. Order matters and is specified: answer action ->
    /// fetch token -> connect -> publish mic -> fulfil (spec 2.2).
    nonisolated func provider(_: CXProvider, perform action: CXAnswerCallAction) {
        Task { @MainActor in
            guard callId != nil else { action.fail(); return }
            configureAudioSession()
            await acceptAndConnect()
            if case .ended = phase { action.fail() } else { action.fulfill() }
        }
    }

    nonisolated func provider(_: CXProvider, perform action: CXEndCallAction) {
        Task { @MainActor in
            let wasRinging = phase == .incomingRinging
            let id = callId
            action.fulfill()

            // Declining a ringing call and hanging up an active one are
            // different server-side transitions.
            if wasRinging, let id {
                _ = try? await APIClient.shared.decline(callId: id)
                await teardown(reason: "declined", notifyServer: false)
            } else {
                await teardown(reason: "ended")
            }
        }
    }

    nonisolated func provider(_: CXProvider, perform action: CXSetMutedCallAction) {
        Task { @MainActor in
            isMuted = action.isMuted
            try? await room?.localParticipant.setMicrophone(enabled: !action.isMuted)
            action.fulfill()
        }
    }

    /// The system has activated the audio session. Only now may the audio
    /// engine run — starting it earlier is what produces silent calls.
    nonisolated func provider(_: CXProvider, didActivate _: AVAudioSession) {
        Task { @MainActor in
            log("audio session activated by CallKit")
            AudioManager.shared.audioSession.isSpeakerOutputPreferred = isSpeaker
            _ = try? AudioManager.shared.setEngineAvailability(.default)
        }
    }

    nonisolated func provider(_: CXProvider, didDeactivate _: AVAudioSession) {
        Task { @MainActor in
            log("audio session deactivated")
            _ = try? AudioManager.shared.setEngineAvailability(.none)
        }
    }
}

// MARK: - RoomDelegate

extension CallCenter: RoomDelegate {
    nonisolated func roomDidConnect(_: Room) {
        Task { @MainActor in log("roomDidConnect") }
    }

    nonisolated func roomIsReconnecting(_: Room) {
        Task { @MainActor in
            // Do not tear the call down. LiveKit reconnects on its own; a short
            // blip should show a pill, not end the call (spec 2.3).
            log("reconnecting…")
            if phase == .active { phase = .reconnecting }
        }
    }

    nonisolated func roomDidReconnect(_: Room) {
        Task { @MainActor in
            log("reconnected")
            if phase == .reconnecting { phase = .active }
        }
    }

    nonisolated func room(_: Room, didDisconnectWithError error: LiveKitError?) {
        Task { @MainActor in
            log("disconnected\(error.map { ": \($0.localizedDescription)" } ?? "")")
            if phase == .active || phase == .reconnecting || phase == .connecting {
                await teardown(reason: "disconnected")
            }
        }
    }

    nonisolated func room(_: Room, participantDidConnect participant: RemoteParticipant) {
        Task { @MainActor in
            // The agent is identified by participant metadata, not by a local
            // flag — the indicator must reflect who is actually in the room.
            if participant.metadata?.contains("\"kind\":\"agent\"") == true {
                agentPresent = true
                log("agent joined")
            } else {
                log("participant joined: \(participant.identity?.stringValue ?? "?")")
            }
        }
    }

    nonisolated func room(_: Room, participantDidDisconnect participant: RemoteParticipant) {
        Task { @MainActor in
            if participant.metadata?.contains("\"kind\":\"agent\"") == true {
                agentPresent = false
                log("agent left")
            } else {
                log("participant left: \(participant.identity?.stringValue ?? "?")")
            }
        }
    }

    /// The real widget transport (spec section 7): reliable data messages on the
    /// "widget" topic, published by the agent. Wired now so Phase 4 only has to
    /// start sending — nothing on the client changes.
    nonisolated func room(
        _: Room,
        participant: RemoteParticipant?,
        didReceiveData data: Data,
        forTopic topic: String,
        encryptionType _: EncryptionType
    ) {
        Task { @MainActor in
            guard topic == "widget" else {
                log("data on unexpected topic '\(topic)', ignored")
                return
            }
            // Humans are minted with canPublishData: false, so anything arriving
            // here should be the agent. Verify rather than assume.
            let isAgent = participant?.metadata?.contains("\"kind\":\"agent\"") == true
            guard isAgent else {
                log("widget from non-agent participant, ignored")
                return
            }
            handleWidget(data: data)
        }
    }

    nonisolated func room(
        _: Room, participant _: Participant, didUpdateConnectionQuality quality: ConnectionQuality
    ) {
        Task { @MainActor in
            connectionQuality = String(describing: quality)
        }
    }
}
