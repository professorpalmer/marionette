import AppKit

final class Fixture: NSObject, NSApplicationDelegate {
    var window: NSWindow!
    let input = NSTextField(frame: NSRect(x: 30, y: 145, width: 270, height: 28))
    let result = NSTextField(labelWithString: "Not saved")
    func applicationDidFinishLaunching(_ notification: Notification) {
        window = NSWindow(contentRect: NSRect(x: 150, y: 250, width: 420, height: 260), styleMask: [.titled, .closable], backing: .buffered, defer: false)
        window.title = "Marionette computer fixture"
        input.setAccessibilityLabel("Fixture name")
        let button = NSButton(title: "Apply fixture", target: self, action: #selector(apply))
        button.frame = NSRect(x: 30, y: 95, width: 160, height: 32)
        result.frame = NSRect(x: 30, y: 50, width: 330, height: 28)
        let password = NSSecureTextField(frame: NSRect(x: 30, y: 195, width: 270, height: 28))
        password.stringValue = "NEVER_SNAPSHOT_PASSWORD"
        for view in [input, button, result, password] { window.contentView?.addSubview(view) }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }
    @objc func apply() { result.stringValue = "Saved " + input.stringValue }
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)
let delegate = Fixture()
app.delegate = delegate
app.run()
