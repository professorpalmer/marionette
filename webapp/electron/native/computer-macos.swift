import Foundation
import AppKit
import ApplicationServices
import ScreenCaptureKit

private let maxElements = 200
private let maxDepth = 12

private struct SnapshotState {
    let id: String
    let appID: String
    let pid: pid_t
    let windowID: CGWindowID
    let window: AXUIElement
    let bounds: CGRect
    let title: String
    let url: String?
    let elements: [String: AXUIElement]
    let screenshotSize: CGSize?
}

private enum HelperError: Error, CustomStringConvertible {
    case message(String)
    var description: String { if case .message(let text) = self { return text }; return "Native computer error" }
}

private final class OutcomeBox: @unchecked Sendable {
    var value: Result<(Int, Int), Error>?
}

private var snapshots: [String: SnapshotState] = [:]
private var screenshotDirectory = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)

private func attribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
    var value: CFTypeRef?
    return AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success ? value : nil
}

private func stringAttribute(_ element: AXUIElement, _ name: String) -> String? {
    if let value = attribute(element, name) as? String { return value }
    if let value = attribute(element, name) as? URL { return value.absoluteString }
    return nil
}

private func rectAttribute(_ element: AXUIElement) -> CGRect? {
    guard let positionValue = attribute(element, kAXPositionAttribute),
          let sizeValue = attribute(element, kAXSizeAttribute),
          CFGetTypeID(positionValue) == AXValueGetTypeID(),
          CFGetTypeID(sizeValue) == AXValueGetTypeID() else { return nil }
    let position = unsafeBitCast(positionValue, to: AXValue.self)
    let size = unsafeBitCast(sizeValue, to: AXValue.self)
    var point = CGPoint.zero
    var dimensions = CGSize.zero
    guard AXValueGetValue(position, .cgPoint, &point), AXValueGetValue(size, .cgSize, &dimensions) else { return nil }
    return CGRect(origin: point, size: dimensions)
}

private func regularApplications() -> [[String: Any]] {
    NSWorkspace.shared.runningApplications.compactMap { application in
        guard application.activationPolicy == .regular,
              !application.isTerminated,
              let bundleID = application.bundleIdentifier,
              let name = application.localizedName else { return nil }
        return ["app_id": "bundle:\(bundleID)", "name": name, "pid": Int(application.processIdentifier), "instance_id": String(application.launchDate?.timeIntervalSince1970 ?? 0)]
    }.sorted { ($0["name"] as? String ?? "").localizedCaseInsensitiveCompare($1["name"] as? String ?? "") == .orderedAscending }
        .prefix(200).map { $0 }
}

private func resolveApplication(_ appID: String) throws -> NSRunningApplication {
    guard appID.hasPrefix("bundle:") else { throw HelperError.message("Invalid macOS app_id") }
    let bundleID = String(appID.dropFirst("bundle:".count))
    let matches = NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).filter { !$0.isTerminated }
    guard matches.count == 1, let application = matches.first else {
        throw HelperError.message(matches.isEmpty ? "The selected app is no longer running" : "Multiple processes share this app_id; select an unambiguous app")
    }
    return application
}

private func focusedWindow(for application: NSRunningApplication) throws -> AXUIElement {
    let appElement = AXUIElementCreateApplication(application.processIdentifier)
    guard let window = attribute(appElement, kAXFocusedWindowAttribute) else {
        throw HelperError.message("The selected app has no focused window")
    }
    return unsafeBitCast(window, to: AXUIElement.self)
}

private func cgWindowID(pid: pid_t, window: AXUIElement) throws -> CGWindowID {
    let targetBounds = rectAttribute(window)
    let targetTitle = stringAttribute(window, kAXTitleAttribute) ?? ""
    guard let rows = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] else {
        throw HelperError.message("Unable to enumerate the selected app window")
    }
    let candidates = rows.filter { ($0[kCGWindowOwnerPID as String] as? Int) == Int(pid) }
    let exact = candidates.filter { row in
        guard let bounds = row[kCGWindowBounds as String] as? [String: Any], let targetBounds else { return false }
        let x = bounds["X"] as? CGFloat ?? .nan
        let y = bounds["Y"] as? CGFloat ?? .nan
        let width = bounds["Width"] as? CGFloat ?? .nan
        let height = bounds["Height"] as? CGFloat ?? .nan
        let title = row[kCGWindowName as String] as? String ?? ""
        return abs(x - targetBounds.origin.x) < 2 && abs(y - targetBounds.origin.y) < 2
            && abs(width - targetBounds.width) < 2 && abs(height - targetBounds.height) < 2
            && (targetTitle.isEmpty || title.isEmpty || title == targetTitle)
    }
    guard exact.count == 1, let number = exact[0][kCGWindowNumber as String] as? UInt32 else {
        throw HelperError.message("Could not identify one exact front window for the selected app")
    }
    return CGWindowID(number)
}

private func safeLabel(_ element: AXUIElement, role: String) -> String {
    if role == "AXSecureTextField" || stringAttribute(element, kAXSubroleAttribute) == "AXSecureTextField" { return stringAttribute(element, kAXTitleAttribute) ?? "" }
    let candidates = [kAXTitleAttribute, kAXDescriptionAttribute, kAXValueAttribute, kAXHelpAttribute]
    for key in candidates {
        if let text = stringAttribute(element, key), !text.isEmpty { return String(text.prefix(160)) }
    }
    return ""
}

private func collectTree(_ root: AXUIElement) -> ([[String: Any]], [String: AXUIElement], String) {
    var rows: [[String: Any]] = []
    var references: [String: AXUIElement] = [:]
    var textRows: [String] = []
    var queue: [(AXUIElement, Int)] = [(root, 0)]
    var cursor = 0
    while cursor < queue.count && rows.count < maxElements {
        let (element, depth) = queue[cursor]
        cursor += 1
        let role = stringAttribute(element, kAXRoleAttribute) ?? "AXUnknown"
        let label = safeLabel(element, role: role)
        if !label.isEmpty { textRows.append(label) }
        let ref = "r\(rows.count + 1)"
        var row: [String: Any] = ["ref": ref, "role": String(role.dropFirst(role.hasPrefix("AX") ? 2 : 0)), "label": label]
        if let bounds = rectAttribute(element), bounds.width > 0, bounds.height > 0 {
            row["bounds"] = ["x": bounds.origin.x, "y": bounds.origin.y, "width": bounds.width, "height": bounds.height]
        }
        rows.append(row)
        references[ref] = element
        if depth < maxDepth, let children = attribute(element, kAXChildrenAttribute) as? [AXUIElement] {
            queue.append(contentsOf: children.prefix(maxElements - queue.count).map { ($0, depth + 1) })
        }
    }
    return (rows, references, String(textRows.joined(separator: "\n").prefix(20_000)))
}

@available(macOS 14.0, *)
private func capture(windowID: CGWindowID, to destination: URL) async throws -> (Int, Int) {
    let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
    guard let window = content.windows.first(where: { $0.windowID == windowID }) else {
        throw HelperError.message("The selected window is not available for capture")
    }
    let scale = NSScreen.main?.backingScaleFactor ?? 2
    let configuration = SCStreamConfiguration()
    configuration.width = max(1, Int(window.frame.width * scale))
    configuration.height = max(1, Int(window.frame.height * scale))
    configuration.showsCursor = false
    configuration.captureResolution = .best
    let image = try await SCScreenshotManager.captureImage(contentFilter: SCContentFilter(desktopIndependentWindow: window), configuration: configuration)
    guard let data = NSBitmapImageRep(cgImage: image).representation(using: .png, properties: [:]) else {
        throw HelperError.message("Could not encode the selected window screenshot")
    }
    try data.write(to: destination, options: .atomic)
    return (image.width, image.height)
}

private func captureSynchronously(windowID: CGWindowID, to destination: URL) throws -> (Int, Int) {
    guard #available(macOS 14.0, *) else { throw HelperError.message("Window screenshots require macOS 14 or newer") }
    let semaphore = DispatchSemaphore(value: 0)
    let outcome = OutcomeBox()
    Task {
        let result: Result<(Int, Int), Error>
        do { result = .success(try await capture(windowID: windowID, to: destination)) }
        catch { result = .failure(error) }
        outcome.value = result
        semaphore.signal()
    }
    semaphore.wait()
    return try outcome.value!.get()
}

private func snapshot(_ request: [String: Any]) throws -> [String: Any] {
    guard AXIsProcessTrusted() else { throw HelperError.message("Accessibility permission is required before inspecting an app") }
    guard let appID = request["app_id"] as? String else { throw HelperError.message("app_id is required") }
    let application = try resolveApplication(appID)
    let window = try focusedWindow(for: application)
    let windowID = try cgWindowID(pid: application.processIdentifier, window: window)
    guard let bounds = rectAttribute(window) else { throw HelperError.message("The selected window has no readable bounds") }
    let (elements, references, text) = collectTree(window)
    let snapshotID = UUID().uuidString
    let destination = screenshotDirectory.appendingPathComponent("snapshot-\(UUID().uuidString).png")
    let title = stringAttribute(window, kAXTitleAttribute) ?? ""
    let url = stringAttribute(window, kAXURLAttribute)
    var response: [String: Any] = [
        "snapshot_id": snapshotID,
        "app_id": appID,
        "window_id": "\(application.processIdentifier):\(windowID)",
        "title": title,
        "text": text,
        "elements": elements,
    ]
    if let url, !url.isEmpty { response["url"] = url }
    var screenshotSize: CGSize?
    if CGPreflightScreenCaptureAccess() {
        let (width, height) = try captureSynchronously(windowID: windowID, to: destination)
        response["screenshot_path"] = destination.path
        response["width"] = width
        response["height"] = height
        response["coordinates"] = "screenshot pixels relative to the selected window; accessibility bounds use desktop points"
        screenshotSize = CGSize(width: width, height: height)
    }
    snapshots.removeAll()
    snapshots[snapshotID] = SnapshotState(id: snapshotID, appID: appID, pid: application.processIdentifier, windowID: windowID, window: window, bounds: bounds, title: title, url: url, elements: references, screenshotSize: screenshotSize)
    return response
}

private func checkedSnapshot(_ request: [String: Any]) throws -> SnapshotState {
    guard AXIsProcessTrusted() else { throw HelperError.message("Accessibility permission is required before controlling an app") }
    guard let appID = request["app_id"] as? String,
          let snapshotID = request["snapshot_id"] as? String,
          let state = snapshots[snapshotID], state.appID == appID else { throw HelperError.message("Stale or unknown snapshot reference") }
    let application = try resolveApplication(appID)
    guard application.processIdentifier == state.pid else { snapshots.removeAll(); throw HelperError.message("The selected process changed") }
    let currentWindow = try focusedWindow(for: application)
    let currentID = try cgWindowID(pid: state.pid, window: currentWindow)
    guard currentID == state.windowID else { snapshots.removeAll(); throw HelperError.message("The selected window changed; take a new snapshot") }
    guard stringAttribute(currentWindow, kAXTitleAttribute) ?? "" == state.title,
          stringAttribute(currentWindow, kAXURLAttribute) == state.url else {
        snapshots.removeAll(); throw HelperError.message("The selected window navigated or changed; take a new snapshot")
    }
    guard rectAttribute(currentWindow) == state.bounds else { snapshots.removeAll(); throw HelperError.message("The window moved or resized; take a new snapshot") }
    application.activate(options: [.activateIgnoringOtherApps])
    AXUIElementPerformAction(currentWindow, kAXRaiseAction as CFString)
    let deadline = Date().addingTimeInterval(1)
    while NSWorkspace.shared.frontmostApplication?.processIdentifier != state.pid && Date() < deadline { Thread.sleep(forTimeInterval: 0.01) }
    guard NSWorkspace.shared.frontmostApplication?.processIdentifier == state.pid,
          try cgWindowID(pid: state.pid, window: focusedWindow(for: application)) == state.windowID else {
        snapshots.removeAll(); throw HelperError.message("The selected window could not receive focus")
    }
    return state
}

private func checkedElement(_ ref: String, state: SnapshotState) throws -> AXUIElement {
    guard let element = state.elements[ref], let window = attribute(element, kAXWindowAttribute),
          CFEqual(window, state.window) else { throw HelperError.message("Stale or out-of-window element reference") }
    var pid: pid_t = 0
    guard AXUIElementGetPid(element, &pid) == .success, pid == state.pid else { throw HelperError.message("Element ownership changed") }
    return element
}

private func postClick(_ point: CGPoint) throws {
    guard let source = CGEventSource(stateID: .hidSystemState),
          let down = CGEvent(mouseEventSource: source, mouseType: .leftMouseDown, mouseCursorPosition: point, mouseButton: .left),
          let up = CGEvent(mouseEventSource: source, mouseType: .leftMouseUp, mouseCursorPosition: point, mouseButton: .left) else {
        throw HelperError.message("Could not create mouse input")
    }
    down.post(tap: .cghidEventTap); up.post(tap: .cghidEventTap)
}

private func click(_ request: [String: Any], state: SnapshotState) throws {
    if let ref = request["ref"] as? String {
        let element = try checkedElement(ref, state: state)
        if AXUIElementPerformAction(element, kAXPressAction as CFString) == .success { return }
        guard let bounds = rectAttribute(element), state.bounds.contains(CGPoint(x: bounds.midX, y: bounds.midY)) else { throw HelperError.message("The element cannot be clicked inside this window") }
        try postClick(CGPoint(x: bounds.midX, y: bounds.midY)); return
    }
    guard let size = state.screenshotSize, let x = request["x"] as? Double, let y = request["y"] as? Double,
          x >= 0, y >= 0, x < size.width, y < size.height else { throw HelperError.message("Click coordinates require a screenshot and must be inside it") }
    try postClick(CGPoint(x: state.bounds.minX + x * state.bounds.width / size.width, y: state.bounds.minY + y * state.bounds.height / size.height))
}

private func typeText(_ request: [String: Any], state: SnapshotState) throws {
    if let ref = request["ref"] as? String {
        let element = try checkedElement(ref, state: state)
        guard AXUIElementSetAttributeValue(element, kAXFocusedAttribute as CFString, kCFBooleanTrue) == .success else {
            throw HelperError.message("The element cannot receive keyboard focus")
        }
    }
    guard let text = request["text"] as? String, let source = CGEventSource(stateID: .hidSystemState) else { throw HelperError.message("Could not create keyboard input") }
    let units = Array(text.utf16)
    for chunk in stride(from: 0, to: units.count, by: 32) {
        let slice = Array(units[chunk..<min(chunk + 32, units.count)])
        guard let down = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: true),
              let up = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: false) else { throw HelperError.message("Could not create keyboard input") }
        down.keyboardSetUnicodeString(stringLength: slice.count, unicodeString: slice)
        up.keyboardSetUnicodeString(stringLength: slice.count, unicodeString: slice)
        down.post(tap: .cghidEventTap); up.post(tap: .cghidEventTap)
    }
}

private let keyCodes: [String: CGKeyCode] = [
    "A": 0, "S": 1, "D": 2, "F": 3, "H": 4, "G": 5, "Z": 6, "X": 7, "C": 8, "V": 9,
    "B": 11, "Q": 12, "W": 13, "E": 14, "R": 15, "Y": 16, "T": 17, "1": 18, "2": 19,
    "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29,
    "]": 30, "O": 31, "U": 32, "[": 33, "I": 34, "P": 35, "RETURN": 36, "L": 37, "J": 38,
    "'": 39, "K": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "N": 45, "M": 46, ".": 47,
    "TAB": 48, "SPACE": 49, "BACKSPACE": 51, "DELETE": 117, "ENTER": 36, "ESCAPE": 53, "LEFT": 123, "RIGHT": 124, "DOWN": 125, "UP": 126,
]

private func keypress(_ request: [String: Any]) throws {
    guard let keys = request["keys"] as? [String], let final = keys.last?.uppercased(), let code = keyCodes[final],
          let source = CGEventSource(stateID: .hidSystemState),
          let down = CGEvent(keyboardEventSource: source, virtualKey: code, keyDown: true),
          let up = CGEvent(keyboardEventSource: source, virtualKey: code, keyDown: false) else { throw HelperError.message("Unsupported key combination") }
    var flags: CGEventFlags = []
    for key in keys.dropLast().map({ $0.uppercased() }) {
        switch key {
        case "CMD", "COMMAND", "META": flags.insert(.maskCommand)
        case "CTRL", "CONTROL": flags.insert(.maskControl)
        case "ALT", "OPTION": flags.insert(.maskAlternate)
        case "SHIFT": flags.insert(.maskShift)
        default: throw HelperError.message("Unsupported modifier \(key)")
        }
    }
    down.flags = flags; up.flags = flags; down.post(tap: .cghidEventTap); up.post(tap: .cghidEventTap)
}

private func scroll(_ request: [String: Any], state: SnapshotState) throws {
    guard let direction = request["direction"] as? String, let source = CGEventSource(stateID: .hidSystemState) else { throw HelperError.message("Invalid scroll request") }
    let amount: Int32 = 500
    let dy: Int32 = direction == "up" ? amount : direction == "down" ? -amount : 0
    let dx: Int32 = direction == "left" ? amount : direction == "right" ? -amount : 0
    CGWarpMouseCursorPosition(CGPoint(x: state.bounds.midX, y: state.bounds.midY))
    guard let event = CGEvent(scrollWheelEvent2Source: source, units: .pixel, wheelCount: 2, wheel1: dy, wheel2: dx, wheel3: 0) else { throw HelperError.message("Could not create scroll input") }
    event.post(tap: .cghidEventTap)
}

private func handle(_ request: [String: Any]) throws -> Any {
    guard let operation = request["operation"] as? String else { throw HelperError.message("operation is required") }
    switch operation {
    case "status":
        return [
            "supported": true,
            "accessibility": AXIsProcessTrusted(),
            "screenRecording": CGPreflightScreenCaptureAccess(),
            "setup": "Enable Marionette in System Settings > Privacy & Security > Accessibility and Screen Recording. Permissions are checked without prompting.",
        ]
    case "apps": return regularApplications()
    case "snapshot": return try snapshot(request)
    case "click", "type", "keypress", "scroll":
        let state = try checkedSnapshot(request)
        defer { snapshots.removeAll() }
        if operation == "click" { try click(request, state: state) }
        else if operation == "type" { try typeText(request, state: state) }
        else if operation == "keypress" { try keypress(request) }
        else { try scroll(request, state: state) }
        return ["ok": true]
    default: throw HelperError.message("Unsupported operation")
    }
}

private func writeResponse(_ response: [String: Any]) {
    guard JSONSerialization.isValidJSONObject(response),
          let data = try? JSONSerialization.data(withJSONObject: response),
          let line = String(data: data, encoding: .utf8) else { return }
    FileHandle.standardOutput.write(Data((line + "\n").utf8))
}

let arguments = CommandLine.arguments
if let index = arguments.firstIndex(of: "--screenshot-dir"), index + 1 < arguments.count {
    screenshotDirectory = URL(fileURLWithPath: arguments[index + 1], isDirectory: true)
}
try? FileManager.default.createDirectory(at: screenshotDirectory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])

while let line = readLine(strippingNewline: true) {
    autoreleasepool {
        var response: [String: Any] = ["id": "", "ok": false]
        do {
            guard line.utf8.count <= 1_048_576,
                  let request = try JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any],
                  let id = request["id"] as? String else { throw HelperError.message("Invalid request") }
            response["id"] = id
            response["result"] = try handle(request)
            response["ok"] = true
        } catch { response["error"] = String(describing: error) }
        writeResponse(response)
    }
}
