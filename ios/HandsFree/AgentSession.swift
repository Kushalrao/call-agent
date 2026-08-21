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

    /// Where the server is, so a failure can say which address it tried.
    var serverAddress: String { Config.baseURL }

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
                session = try await APIClient.shared.startAgentSession()
            } catch {
                // Name the step. "Couldn't reach the copilot" sent us looking at
                // ElevenLabs when the phone simply could not see the Mac.
                fail("Can't reach the hands-free server at \(Config.baseURL). "
                     + "Check the address in Settings and that both devices are "
                     + "on the same network.")
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
                self.lastError = "The copilot didn't join. Check the ElevenLabs "
                    + "account has conversation credits left."
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
