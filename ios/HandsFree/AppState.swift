import Foundation
import SwiftUI

/// Session, directory, and the wiring between the ringing socket and CallKit.
@MainActor
final class AppState: ObservableObject {
    @Published var session: SessionResponse?
    @Published var contacts: [Contact] = []
    @Published var socketConnected = false
    @Published var loginError: String?
    @Published var isLoggingIn = false

    let callCenter = CallCenter()
    private let socket = EventSocket()

    private static let sessionKey = "session_token"
    private static let userIdKey = "session_user_id"
    private static let userNameKey = "session_user_name"
    private static let livekitKey = "session_livekit_url"

    init() {
        wireSocket()
        restoreSession()
    }

    var isSignedIn: Bool { session != nil }

    // MARK: - Auth

    func logIn(code: String, displayName: String, baseURL: String) async {
        isLoggingIn = true
        loginError = nil
        Config.baseURL = baseURL.trimmingCharacters(in: .whitespacesAndNewlines)

        do {
            let s = try await APIClient.shared.devLogin(
                code: code.trimmingCharacters(in: .whitespacesAndNewlines).uppercased(),
                displayName: displayName.trimmingCharacters(in: .whitespacesAndNewlines)
            )
            await APIClient.shared.setToken(s.accessToken)
            persist(s)
            session = s
            socket.start(token: s.accessToken)
            await refreshContacts()
        } catch {
            loginError = error.localizedDescription
        }
        isLoggingIn = false
    }

    func logOut() {
        socket.stop()
        Task { await APIClient.shared.setToken(nil) }
        let d = UserDefaults.standard
        [Self.sessionKey, Self.userIdKey, Self.userNameKey, Self.livekitKey].forEach {
            d.removeObject(forKey: $0)
        }
        session = nil
        contacts = []
    }

    private func persist(_ s: SessionResponse) {
        let d = UserDefaults.standard
        d.set(s.accessToken, forKey: Self.sessionKey)
        d.set(s.userId, forKey: Self.userIdKey)
        d.set(s.displayName, forKey: Self.userNameKey)
        d.set(s.livekitUrl, forKey: Self.livekitKey)
    }

    private func restoreSession() {
        let d = UserDefaults.standard
        guard
            let token = d.string(forKey: Self.sessionKey),
            let userId = d.string(forKey: Self.userIdKey),
            let name = d.string(forKey: Self.userNameKey)
        else { return }

        let s = SessionResponse(
            accessToken: token,
            userId: userId,
            displayName: name,
            livekitUrl: d.string(forKey: Self.livekitKey) ?? ""
        )
        session = s
        Task {
            await APIClient.shared.setToken(token)
            socket.start(token: token)
            await refreshContacts()
        }
    }

    // MARK: - Directory

    func refreshContacts() async {
        guard isSignedIn else { return }
        do {
            contacts = try await APIClient.shared.contacts()
        } catch {
            // A stale token after a server restart shows up here first.
            if case let APIError.http(status, _) = error, status == 401 {
                logOut()
                loginError = "Session expired — log in again."
            }
        }
    }

    // MARK: - Ringing

    private func wireSocket() {
        socket.onConnectionChange = { [weak self] connected in
            self?.socketConnected = connected
        }

        socket.onEvent = { [weak self] event in
            guard let self else { return }
            switch event {
            case let .hello(userId):
                callCenter.log("ringing socket ready (\(userId.prefix(8)))")

            case let .incomingCall(callId, _, callerName, _):
                // Identical handoff to what the PushKit handler will do in
                // Phase 7 — that is the whole point of routing it through here.
                callCenter.reportIncomingCall(callId: callId, callerName: callerName)

                #if DEBUG
                    // Dev-only test hook: launch with `-auto_answer 1` and the
                    // app answers itself. Needed because the Simulator cannot be
                    // tapped from a script, and it is how the Phase 2 fake-caller
                    // harness will drive a real call end to end.
                    if UserDefaults.standard.bool(forKey: "auto_answer") {
                        Task {
                            try? await Task.sleep(for: .milliseconds(600))
                            await self.callCenter.answerFromApp()
                        }
                    }
                #endif

            case let .callAccepted(callId):
                Task { await self.callCenter.calleeAccepted(callId: callId) }

            case let .callDeclined(callId):
                callCenter.remoteEnded(callId: callId, reason: "declined")

            case let .callEnded(callId):
                callCenter.remoteEnded(callId: callId, reason: "ended")

            case let .callTimeout(callId):
                callCenter.remoteEnded(callId: callId, reason: "timeout")

            case let .unknown(type):
                // Forward compatibility: a newer server may send event types
                // this build has never heard of. Drop and log, never crash.
                callCenter.log("unknown event: \(type)")
            }
        }
    }
}
