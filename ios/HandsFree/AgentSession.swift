import AVFoundation
import Combine
import LiveKit
import SwiftUI

/// A conversation with the ElevenLabs agent.
///
/// Separate from `CallCenter` on purpose. That type exists to run a call between
/// two humans — CallKit, ringing, a second participant, our own control plane's
/// call lifecycle. None of that applies here: there is one human, no ringing, and
/// the far end is an agent that answers immediately. Retrofitting it would have
/// meant threading "is this a real call or an agent session?" through every
/// method, and the two would have started breaking each other.
///
/// The transport is LiveKit, because that is what ElevenLabs' agent platform
/// uses. So this needs no new dependency — same SDK the app already links, a
/// different host and a token minted by our server.
///
/// The token is minted server-side and nothing here ever sees an ElevenLabs API
/// key: a key inside an app bundle is a published key.
enum AgentSessionError: Error {
    case connectTimedOut
    case tokenRefused
}

@MainActor
final class AgentSession: ObservableObject {
    enum Phase: Equatable {
        case idle
        /// Asking our own server for a room token. Fails when the phone cannot
        /// reach the Mac — a different problem, and a different fix, from failing
        /// to reach ElevenLabs. Collapsing the two into one "connecting" state
        /// cost three debugging cycles.
        case requestingToken
        case connecting
        /// Connected and listening, but the agent has not joined the room yet.
        case waitingForAgent
        case live
        case ended(String)
    }

    @Published private(set) var phase: Phase = .idle
    @Published private(set) var lastError: String?
    /// True once the agent's own audio track is subscribed — the only honest
    /// signal that it can actually be heard.
    @Published private(set) var agentAudible = false
    @Published private(set) var isMuted = false
    @Published private(set) var elapsed = 0

    private var room: Room?
    private var conversationID: String?
    private var ticker: Task<Void, Never>?

    var isActive: Bool {
        switch phase {
        case .idle, .ended: return false
        default: return true
        }
    }

    // MARK: - Lifecycle

    func start() async {
        guard !isActive else { return }
        phase = .requestingToken
        lastError = nil
        agentAudible = false
        elapsed = 0

        // Asked before anything is in flight. A denied microphone does not throw
        // later — the session simply joins and publishes nothing, which looks
        // exactly like an agent that cannot hear you.
        guard await requestMicrophone() else {
            fail("hands-free needs microphone access. Enable it in Settings.")
            return
        }

        do {
            let session: AgentSessionInfo
            do {
                // Straight to ElevenLabs. This used to go through our own server
                // so the API key could stay off the device — but the agent is
                // public, so no key is involved, and routing it through the Mac
                // meant the phone had to be on the same network as a laptop. When
                // that laptop changed Wi-Fi the whole feature died, and the error
                // pointed at the copilot instead of at the address.
                //
                // Now the phone needs nothing but internet.
                session = try await fetchAgentToken()
            } catch {
                fail("Couldn't reach ElevenLabs. Check the connection.")
                return
            }
            phase = .connecting
            let room = Room()
            room.add(delegate: self)
            self.room = room
            conversationID = session.conversationId

            // The agent's own voice arrives on a subscribed track, so playback
            // has to be routed and running before it speaks.
            configureAudio()

            // A bounded connect. WebRTC needs UDP, and on a constrained
            // network signalling can succeed while media never establishes —
            // which showed up as a screen stuck on "Connecting" forever with no
            // error to act on. Better to fail and say so.
            try await withThrowingTaskGroup(of: Void.self) { group in
                group.addTask {
                    try await room.connect(
                        url: session.livekitUrl,
                        token: session.token,
                        connectOptions: ConnectOptions(autoSubscribe: true)
                    )
                }
                group.addTask {
                    try await Task.sleep(for: .seconds(15))
                    throw AgentSessionError.connectTimedOut
                }
                try await group.next()
                group.cancelAll()
            }
            try await room.localParticipant.setMicrophone(
                enabled: true,
                captureOptions: AudioCaptureOptions(
                    echoCancellation: true,
                    autoGainControl: true,
                    noiseSuppression: true
                )
            )
            guard !room.localParticipant.audioTracks.isEmpty else {
                fail("Couldn't turn on your microphone.")
                return
            }

            // Tell the agent to begin.
            //
            // This is what was missing. Connecting to the room is not enough:
            // the agent waits for the client to open the conversation, and
            // without it the call sits there. Two attempts showed up in
            // ElevenLabs as 30-second conversations with **zero messages** —
            // a room that connected fine and an agent that never said its first
            // line, because nobody had told it to start.
            try await sendConversationInit(room)

            phase = .waitingForAgent
            startTicking()
            watchForAgent()
        } catch AgentSessionError.connectTimedOut {
            fail("Couldn't reach the copilot. Check the network and try again.")
        } catch is CancellationError {
            // The user backed out while connecting. Not a failure.
            await stop(reason: "cancelled")
        } catch {
            fail("Couldn't start the agent: \(error.localizedDescription)")
        }
    }

    func stop(reason: String = "ended") async {
        ticker?.cancel()
        ticker = nil
        if let room {
            await room.disconnect()
            room.remove(delegate: self)
        }
        room = nil
        agentAudible = false
        phase = .ended(reason)
        // Back to idle so the dial screen returns without a stale end state.
        try? await Task.sleep(for: .milliseconds(400))
        if case .ended = phase { phase = .idle }
    }

    func toggleMute() {
        isMuted.toggle()
        let muted = isMuted
        Task { try? await room?.localParticipant.setMicrophone(enabled: !muted) }
    }

    /// The opening handshake ElevenLabs' own SDKs send once the room is joined.
    ///
    /// Reliable delivery, because a dropped one means a conversation that never
    /// starts rather than a glitch.
    private func sendConversationInit(_ room: Room) async throws {
        let payload: [String: Any] = [
            "type": "conversation_initiation_client_data",
            // No overrides: the prompt, voice and first message are configured
            // on the agent (scripts/provision_agent.py) rather than sent from a
            // client, so a phone build cannot drift from what was provisioned.
            "conversation_config_override": [:] as [String: Any],
            "custom_llm_extra_body": [:] as [String: Any],
            "dynamic_variables": [:] as [String: Any],
        ]
        let data = try JSONSerialization.data(withJSONObject: payload)
        try await room.localParticipant.publish(
            data: data,
            options: DataPublishOptions(reliable: true)
        )
        log("sent conversation_initiation_client_data")
    }

    /// Mint a conversation token directly. Public agent, so no key.
    private func fetchAgentToken() async throws -> AgentSessionInfo {
        var components = URLComponents(string: Config.agentTokenURL)!
        components.queryItems = [URLQueryItem(name: "agent_id", value: Config.agentID)]
        var request = URLRequest(url: components.url!)
        request.timeoutInterval = 15

        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
            throw AgentSessionError.tokenRefused
        }
        struct Minted: Decodable {
            let token: String
            let conversationId: String?
        }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let minted = try decoder.decode(Minted.self, from: data)
        return AgentSessionInfo(
            token: minted.token,
            // ElevenLabs runs its agent rooms on its own LiveKit deployment.
            livekitUrl: "wss://livekit.rtc.elevenlabs.io",
            conversationId: minted.conversationId ?? ""
        )
    }

    // MARK: - Audio

    /// Configure and route. Unlike the two-party path there is no CallKit here,
    /// so nothing else sets the category, enables the engine, or picks an output
    /// — and left alone, playback lands on the earpiece, which is
    /// indistinguishable from an agent that never spoke.
    private func configureAudio() {
        let session = AVAudioSession.sharedInstance()
        var options: AVAudioSession.CategoryOptions = [.defaultToSpeaker]
        if #available(iOS 26.0, *) {
            options.insert(.allowBluetoothHFP)
        } else {
            options.insert(.allowBluetooth)
        }
        do {
            try session.setCategory(.playAndRecord, mode: .voiceChat, options: options)
        } catch {
            // Not fatal: LiveKit's own configuration may still produce audio.
            lastError = nil
        }
        AudioManager.shared.audioSession.isSpeakerOutputPreferred = true
        _ = try? AudioManager.shared.setEngineAvailability(.default)
    }

    private func requestMicrophone() async -> Bool {
        if #available(iOS 17.0, *) {
            switch AVAudioApplication.shared.recordPermission {
            case .granted: return true
            case .denied: return false
            case .undetermined: return await AVAudioApplication.requestRecordPermission()
            @unknown default: return false
            }
        }
        let session = AVAudioSession.sharedInstance()
        switch session.recordPermission {
        case .granted: return true
        case .denied: return false
        case .undetermined:
            return await withCheckedContinuation { continuation in
                session.requestRecordPermission { continuation.resume(returning: $0) }
            }
        @unknown default: return false
        }
    }

    /// The room accepted us but no agent arrived. Usually an ElevenLabs-side
    /// problem — out of conversation credits, or the agent failed to start — and
    /// silently waiting forever tells the user nothing.
    private func watchForAgent() {
        Task { [weak self] in
            try? await Task.sleep(for: .seconds(12))
            guard let self, case .waitingForAgent = self.phase else { return }
            await MainActor.run {
                // Deliberately not naming a cause. The first version of this
                // blamed conversation credits, and the credits were fine — the
                // agent was waiting to be told to start. A guess in an error
                // message sends the next person down the wrong path.
                self.lastError = "The copilot joined the room but never spoke."
            }
        }
    }

    private func startTicking() {
        ticker?.cancel()
        ticker = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(1))
                guard let self, self.isActive else { return }
                await MainActor.run { self.elapsed += 1 }
            }
        }
    }

    private func log(_ message: String) {
        #if DEBUG
            print("[agent] \(message)")
        #endif
    }

    private func fail(_ message: String) {
        lastError = message
        Task { await stop(reason: "failed") }
    }
}

// MARK: - RoomDelegate

extension AgentSession: RoomDelegate {
    nonisolated func room(_: Room, participantDidConnect participant: RemoteParticipant) {
        Task { @MainActor in
            // Any remote participant in an ElevenLabs conversation room is the
            // agent; there is nobody else in it.
            if case .waitingForAgent = phase { phase = .live }
        }
    }

    nonisolated func room(
        _: Room,
        participant _: RemoteParticipant,
        didSubscribeTrack publication: RemoteTrackPublication
    ) {
        Task { @MainActor in
            guard publication.kind == .audio else { return }
            // Subscribed is the earliest point at which the agent can be heard.
            // Without this the UI would claim it was talking before playback
            // existed, which is the exact confusion that cost us a day.
            agentAudible = true
            if case .waitingForAgent = phase { phase = .live }
        }
    }

    nonisolated func room(
        _: Room,
        participant _: RemoteParticipant,
        didUnsubscribeTrack publication: RemoteTrackPublication
    ) {
        Task { @MainActor in
            if publication.kind == .audio { agentAudible = false }
        }
    }

    nonisolated func room(_: Room, didDisconnectWithError error: LiveKitError?) {
        Task { @MainActor in
            if let error { lastError = error.localizedDescription }
            await stop(reason: error == nil ? "ended" : "disconnected")
        }
    }
}
