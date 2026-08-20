import Foundation

// MARK: - Wire models
//
// The server speaks snake_case; the decoder converts, so these stay idiomatic
// Swift and there are no CodingKeys to drift out of sync.

struct SessionResponse: Codable {
    let accessToken: String
    let userId: String
    let displayName: String
    let livekitUrl: String
}

struct Contact: Codable, Identifiable, Hashable {
    let id: String
    let displayName: String
    let avatarUrl: String?
    let online: Bool
}

struct CallInfo: Codable {
    let callId: String
    let room: String
    let state: String
    let callerId: String
    let calleeId: String
    let lkToken: String?
    let livekitUrl: String?
}

struct CallStateInfo: Codable {
    let callId: String
    let state: String
}

// MARK: - Errors

enum APIError: LocalizedError {
    case badURL
    case http(status: Int, body: String)
    case decoding(String)
    case transport(String)

    var errorDescription: String? {
        switch self {
        case .badURL:
            return "That server address doesn't look right."
        case let .http(status, body):
            if status == 401 { return "That code wasn't recognised." }
            if status == 429 { return "Too many attempts — wait a moment." }
            return "Server error \(status): \(body)"
        case let .decoding(detail):
            return "Unexpected response from server (\(detail))"
        case let .transport(detail):
            return "Can't reach the server: \(detail)"
        }
    }
}

// MARK: - Client

/// Thin async wrapper over the control plane. One method per endpoint, no
/// caching, no retries — the call flow is short enough that a failure should
/// surface immediately rather than being papered over.
actor APIClient {
    static let shared = APIClient()

    private var token: String?

    private let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()

    private let encoder: JSONEncoder = {
        let e = JSONEncoder()
        e.keyEncodingStrategy = .convertToSnakeCase
        return e
    }()

    func setToken(_ token: String?) { self.token = token }

    // MARK: auth

    func devLogin(code: String, displayName: String) async throws -> SessionResponse {
        try await post(
            "/v1/auth/dev-login",
            body: ["user_code": code, "display_name": displayName],
            authorized: false
        )
    }

    // MARK: directory

    func contacts() async throws -> [Contact] {
        try await get("/v1/users/contacts")
    }

    // MARK: calls

    func createCall(calleeId: String) async throws -> CallInfo {
        try await post("/v1/calls", body: ["callee_id": calleeId])
    }

    func accept(callId: String) async throws -> CallInfo {
        try await post("/v1/calls/\(callId)/accept", body: [String: String]())
    }

    func decline(callId: String) async throws -> CallStateInfo {
        try await post("/v1/calls/\(callId)/decline", body: [String: String]())
    }

    func end(callId: String) async throws -> CallStateInfo {
        try await post("/v1/calls/\(callId)/end", body: [String: String]())
    }

    /// Reports that room.connect() succeeded. Both parties reporting is what
    /// moves the call to ACTIVE and dispatches the agent.
    func reportJoined(callId: String) async throws -> CallStateInfo {
        try await post("/v1/calls/\(callId)/joined", body: [String: String]())
    }

    /// Re-mint a join token. The join window is only 120s, so a slow connect
    /// refetches rather than failing the call.
    func refreshToken(callId: String) async throws -> CallInfo {
        try await post("/v1/calls/\(callId)/token", body: [String: String]())
    }

    // MARK: - transport

    private func get<R: Decodable>(_ path: String) async throws -> R {
        guard let url = Config.api(path) else { throw APIError.badURL }
        var req = URLRequest(url: url)
        req.httpMethod = "GET"
        applyAuth(&req)
        return try await send(req)
    }

    private func post<R: Decodable>(
        _ path: String,
        body: [String: String],
        authorized: Bool = true
    ) async throws -> R {
        guard let url = Config.api(path) else { throw APIError.badURL }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try encoder.encode(body)
        if authorized { applyAuth(&req) }
        return try await send(req)
    }

    private func applyAuth(_ req: inout URLRequest) {
        if let token {
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
    }

    private func send<R: Decodable>(_ req: URLRequest) async throws -> R {
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await URLSession.shared.data(for: req)
        } catch {
            throw APIError.transport(error.localizedDescription)
        }

        guard let http = response as? HTTPURLResponse else {
            throw APIError.transport("no HTTP response")
        }
        guard (200 ..< 300).contains(http.statusCode) else {
            throw APIError.http(
                status: http.statusCode,
                body: String(data: data, encoding: .utf8) ?? ""
            )
        }

        // 204 and other empty successes: nothing to decode.
        if data.isEmpty, let empty = EmptyResponse() as? R { return empty }

        do {
            return try decoder.decode(R.self, from: data)
        } catch {
            throw APIError.decoding(String(describing: error))
        }
    }
}

struct EmptyResponse: Codable {}
