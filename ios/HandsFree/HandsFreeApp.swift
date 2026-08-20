import SwiftUI

@main
struct HandsFreeApp: App {
    @StateObject private var app = AppState()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(app)
                .environmentObject(app.callCenter)
                .preferredColorScheme(.dark)
        }
    }
}
