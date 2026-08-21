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

    /// The ElevenLabs agent the app talks to.
    ///
    /// Safe to ship because the agent is public (`enable_auth: false`): the phone
    /// starts a conversation with the agent id alone, no API key involved. That is
    /// deliberate — routing this through our own server meant the phone had to be
    /// able to see the Mac, and a laptop that changed Wi-Fi took the whole feature
    /// down with it.
    ///
    /// The tradeoff, stated plainly: anyone holding this id can talk to the agent
    /// and spend the account's conversation credits. Fine for a dev build with one
    /// user. Before this ships to strangers, turn on authentication in ElevenLabs
    /// and go back to server-minted tokens — the endpoint for that already exists
    /// at POST /v1/agent/session.
    static let agentID = "agent_2401kyefrv9pehcsef086nb664eg"

    /// Where ElevenLabs mints conversation tokens. Public agents need no key.
    static let agentTokenURL = "https://api.elevenlabs.io/v1/convai/conversation/token"

    /// Where the flight tool lives. A cloudflare quick tunnel in dev, so it
    /// changes on every restart — hence a UserDefaults override rather than only
    /// a constant. Only the public /session/latest endpoint is read from the app;
    /// the search endpoint keeps its secret and is called by ElevenLabs, not by us.
    private static let flightToolKey = "flight_tool_base_url"
    private static let defaultFlightToolURL = "https://ltd-here-rendering-retained.trycloudflare.com"

    static var flightToolURL: String {
        get { UserDefaults.standard.string(forKey: flightToolKey) ?? defaultFlightToolURL }
        set { UserDefaults.standard.set(newValue, forKey: flightToolKey) }
    }

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
