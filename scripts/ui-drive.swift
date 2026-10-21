import Cocoa

// ui-drive.swift — accessibility driver for the SteamOS Devkit app (and any
// macOS app). Built with CLT swiftc; run from a TCC-granted Terminal context.
//
//   ui-drive <pid> probe
//       Print grants (AX trusted, screen recording) and on-screen windows.
//   ui-drive <pid> dump [maxDepth]
//       Print the AX tree: role, title, value, enabled.
//   ui-drive <pid> click <title-or-label> [--role AXButton]
//       BFS the tree for the first element whose title/label/value matches;
//       AXPress it (or its nearest pressable ancestor).
//   ui-drive <pid> type <title-or-label> <text>
//       Focus the matching field and set its AXValue.
//   ui-drive <pid> shot <out.png> <windowID>
//       Capture the window (needs Screen Recording).
//   ui-drive <pid> wait <seconds>
//       Sleep (lets the app react between steps).
//
// Exit 0 on success; 2 when a grant is missing; 3 when a target isn't found.

struct Grants {
    static func checkAX() -> Bool { AXIsProcessTrusted() }
    static func checkScreen() -> Bool { CGPreflightScreenCaptureAccess() }
}

func elementAttrs(_ el: AXUIElement) -> (
    role: String, title: String?, value: String?, label: String?, enabled: Bool
) {
    func str(_ attr: String) -> String? {
        var v: CFTypeRef?
        AXUIElementCopyAttributeValue(el, attr as CFString, &v)
        if let s = v as? String { return s }
        if let n = v as? NSNumber { return n.stringValue }
        return nil
    }
    var enabledRef: CFTypeRef?
    AXUIElementCopyAttributeValue(el, kAXEnabledAttribute as CFString, &enabledRef)
    return (
        str(kAXRoleAttribute) ?? "?",
        str(kAXTitleAttribute),
        str(kAXValueAttribute),
        str("AXLabel"),
        (enabledRef as? Bool) ?? true
    )
}

func children(of el: AXUIElement) -> [AXUIElement] {
    var v: CFTypeRef?
    AXUIElementCopyAttributeValue(el, kAXChildrenAttribute as CFString, &v)
    return (v as? [AXUIElement]) ?? []
}

func dump(_ el: AXUIElement, _ depth: Int, _ maxDepth: Int) {
    guard depth <= maxDepth else { return }
    let a = elementAttrs(el)
    var line = String(repeating: "  ", count: depth) + a.role
    if let t = a.title, !t.isEmpty { line += " title=\"\(t)\"" }
    if let l = a.label, !l.isEmpty, l != a.title { line += " label=\"\(l)\"" }
    if let v = a.value, !v.isEmpty { line += " value=\"\(v.prefix(100))\"" }
    if !a.enabled { line += " [disabled]" }
    print(line)
    for kid in children(of: el) { dump(kid, depth + 1, maxDepth) }
}

/// Breadth-first search for the first element matching text in title/label/value.
func find(_ root: AXUIElement, _ needle: String, role: String?) -> AXUIElement? {
    var queue = [root]
    var visited = 0
    while !queue.isEmpty, visited < 20000 {
        let el = queue.removeFirst()
        visited += 1
        let a = elementAttrs(el)
        let texts = [a.title, a.label, a.value].compactMap { $0 }
        let matches = texts.contains { $0 == needle || $0.contains(needle) }
        if matches && (role == nil || a.role == role) { return el }
        queue.append(contentsOf: children(of: el))
    }
    return nil
}

/// Nearest ancestor (or self) supporting AXPress.
func pressable(_ el: AXUIElement) -> AXUIElement {
    var current: CFTypeRef = el
    while true {
        let ui = current as! AXUIElement
        var actions: CFArray?
        AXUIElementCopyActionNames(ui, &actions)
        if let names = actions as? [String], names.contains("AXPress") { return ui }
        var parent: CFTypeRef?
        AXUIElementCopyAttributeValue(ui, kAXParentAttribute as CFString, &parent)
        guard let p = parent else { return ui }
        current = p
    }
}

let args = CommandLine.arguments
guard args.count >= 3, let pid = pid_t(args[1]) else {
    FileHandle.standardError.write("usage: ui-drive <pid> <command> ...\n".data(using: .utf8)!)
    exit(64)
}
let command = args[2]
let app = AXUIElementCreateApplication(pid)

switch command {
case "probe":
    print("ax-trusted: \(Grants.checkAX())")
    print("screen-recording: \(Grants.checkScreen())")
    if let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly], kCGNullWindowID) as? [[String: Any]] {
        for w in list where (w["kCGWindowOwnerPID"] as? Int32) == pid {
            print("window: \(w["kCGWindowNumber"] as? Int ?? -1) \"\(w["kCGWindowTitle"] as? String ?? "")\"")
        }
    }
    if !Grants.checkAX() { exit(2) }

case "dump":
    guard Grants.checkAX() else { FileHandle.standardError.write("AX not trusted\n".data(using: .utf8)!); exit(2) }
    let maxDepth = args.count > 3 ? Int(args[3]) ?? 6 : 6
    dump(app, 0, maxDepth)

case "click":
    guard Grants.checkAX() else { FileHandle.standardError.write("AX not trusted\n".data(using: .utf8)!); exit(2) }
    let needle = args[3]
    var role: String?
    if args.count > 5, args[4] == "--role" { role = args[5] }
    guard let found = find(app, needle, role: role) else {
        FileHandle.standardError.write("not found: \(needle)\n".data(using: .utf8)!)
        exit(3)
    }
    let target = pressable(found)
    let a = elementAttrs(target)
    AXUIElementPerformAction(target, "AXPress" as CFString)
    print("clicked \(a.role) title=\"\(a.title ?? "")\"")

case "type":
    guard Grants.checkAX() else { FileHandle.standardError.write("AX not trusted\n".data(using: .utf8)!); exit(2) }
    let needle = args[3]
    let text = args[4]
    guard let found = find(app, needle, role: nil) else {
        FileHandle.standardError.write("not found: \(needle)\n".data(using: .utf8)!)
        exit(3)
    }
    AXUIElementSetAttributeValue(found, kAXFocusedAttribute as CFString, true as CFTypeRef)
    usleep(200_000)
    AXUIElementSetAttributeValue(found, kAXValueAttribute as CFString, text as CFTypeRef)
    print("typed into \(elementAttrs(found).role) title=\"\(needle)\"")

case "shot":
    guard Grants.checkScreen() else {
        FileHandle.standardError.write("screen recording not granted\n".data(using: .utf8)!)
        exit(2)
    }
    let out = args[3]
    guard let windowID = UInt32(args[4]) else { exit(64) }
    guard let image = CGWindowListCreateImage(
        .null, .optionIncludingWindow, windowID, [.boundsIgnoreFraming, .bestResolution]
    ) else {
        FileHandle.standardError.write("capture failed\n".data(using: .utf8)!)
        exit(3)
    }
    let rep = NSBitmapImageRep(cgImage: image)
    guard let png = rep.representation(using: .png, properties: [:]) else { exit(3) }
    try png.write(to: URL(fileURLWithPath: out))
    print("wrote \(out) (\(image.width)x\(image.height))")

case "wait":
    usleep(UInt32((Double(args[3]) ?? 1) * 1_000_000))

default:
    FileHandle.standardError.write("unknown command: \(command)\n".data(using: .utf8)!)
    exit(64)
}
