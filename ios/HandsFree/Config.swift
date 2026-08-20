import Foundation

/// Where the control plane lives.
///
/// Editable in the app because the dev server's LAN address changes with the
/// network, and hardcoding it means a rebuild every time you move desks.
enum Config {
    private static let baseURLKey = "control_plane_base_url"

    /// Default for the Simulator, which shares the Mac's loopback. On a real
    /// phone this must be the Mac's LAN address (e.g. http://192.168.1.42:8000),
    /// set on the login screen.
    static let defaultBaseURL = "http://127.0.0.1:8000"

    static var baseURL: String {
        get { UserDefaults.standard.string(forKey: baseURLKey) ?? defaultBaseURL }
        set { UserDefaults.standard.set(newValue, forKey: baseURLKey) }
    }

    /// The ringing WebSocket, derived from the base URL so there is one source
    /// of truth for the host.
    static func eventsURL(token: String) -> URL? {
        guard var comps = URLComponents(string: baseURL) else { return nil }
        comps.scheme = comps.scheme == "https" ? "wss" : "ws"
        comps.path = "/v1/events"
        comps.queryItems = [URLQueryItem(name: "token", value: token)]
        return comps.url
    }

    static func api(_ path: String) -> URL? {
        URL(string: baseURL + path)
    }
}
