import Foundation

/// Incoming events from the control plane (spec 2.2b).
enum ServerEvent {
    case hello(userId: String)
    case incomingCall(callId: String, callerId: String, callerName: String, room: String)
    case callAccepted(callId: String)
    case callDeclined(callId: String)
    case callEnded(callId: String)
    case callTimeout(callId: String)
    case unknown(type: String)

    init?(json: [String: Any]) {
        guard let type = json["type"] as? String else { return nil }
        let callId = json["call_id"] as? String ?? ""
        switch type {
        case "hello":
            self = .hello(userId: json["user_id"] as? String ?? "")
        case "incoming_call":
            self = .incomingCall(
                callId: callId,
                callerId: json["caller_id"] as? String ?? "",
                callerName: json["caller_name"] as? String ?? "Unknown",
                room: json["room"] as? String ?? ""
            )
        case "call_accepted": self = .callAccepted(callId: callId)
        case "call_declined": self = .callDeclined(callId: callId)
        case "call_ended": self = .callEnded(callId: callId)
        case "call_timeout": self = .callTimeout(callId: callId)
        default: self = .unknown(type: type)
        }
    }
}

/// The dev ringing transport.
///
/// In Phase 7 this is replaced by PushKit. The swap is deliberately shallow:
/// this class's only job is to hand a `.incomingCall` to CallCenter, and the
/// push handler will hand over the identical value — so nothing downstream
/// changes, including the CallKit path.
final class EventSocket: NSObject {
    private var task: URLSessionWebSocketTask?
    private var session: URLSession?
    private var token: String?
    private var isStopped = true
    private var retryCount = 0

    /// Called on the main actor for every decoded event.
    var onEvent: ((ServerEvent) -> Void)?
    var onConnectionChange: ((Bool) -> Void)?

    func start(token: String) {
        self.token = token
        isStopped = false
        retryCount = 0
        connect()
    }

    func stop() {
        isStopped = true
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
        notifyConnection(false)
    }

    private func connect() {
        guard !isStopped, let token, let url = Config.eventsURL(token: token) else { return }

        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 0  // long-lived socket
        let session = URLSession(configuration: config)
        self.session = session

        let task = session.webSocketTask(with: url)
        self.task = task
        task.resume()
        receive()

        // Keep NATs and the server's idle timeouts from silently dropping us.
        schedulePing()
    }

    private func receive() {
        task?.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case let .success(message):
                self.retryCount = 0
                self.notifyConnection(true)
                self.handle(message)
                self.receive()
            case .failure:
                // Any read failure means the socket is gone. Reconnecting is
                // safe: the server treats a new socket as an additional
                // registration and prunes dead ones on next send.
                self.notifyConnection(false)
                self.scheduleReconnect()
            }
        }
    }

    private func handle(_ message: URLSessionWebSocketTask.Message) {
        var data: Data?
        switch message {
        case let .string(text): data = text.data(using: .utf8)
        case let .data(raw): data = raw
        @unknown default: break
        }
        guard
            let data,
            let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let event = ServerEvent(json: json)
        else { return }

        DispatchQueue.main.async { self.onEvent?(event) }
    }

    private func schedulePing() {
        DispatchQueue.global().asyncAfter(deadline: .now() + 20) { [weak self] in
            guard let self, !self.isStopped else { return }
            self.task?.sendPing { _ in }
            self.schedulePing()
        }
    }

    private func scheduleReconnect() {
        guard !isStopped else { return }
        retryCount += 1
        // Capped exponential backoff: fast enough that a brief blip is
        // invisible, slow enough not to hammer a server that is actually down.
        let delay = min(pow(2.0, Double(min(retryCount, 5))), 20.0)
        DispatchQueue.global().asyncAfter(deadline: .now() + delay) { [weak self] in
            self?.connect()
        }
    }

    private func notifyConnection(_ connected: Bool) {
        DispatchQueue.main.async { self.onConnectionChange?(connected) }
    }
}
