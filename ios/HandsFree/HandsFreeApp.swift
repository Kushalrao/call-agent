import SwiftUI

@main
struct HandsFreeApp: App {
    @StateObject private var app = AppState()
    // The agent conversation is its own object, not part of CallCenter: one
    // human, no ringing, no CallKit. Threading "is this a call or an agent?"
    // through CallCenter would have had the two breaking each other.
    @StateObject private var agent = AgentSession()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(app)
                .environmentObject(app.callCenter)
                .environmentObject(agent)
                .preferredColorScheme(.dark)
        }
    }
}
