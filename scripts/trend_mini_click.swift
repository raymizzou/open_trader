import AppKit
import CoreGraphics

// Temporary diagnostic: one observed window, one bounded click; no retries.
func point(_ x: Double, _ y: Double, in bounds: CGRect) -> CGPoint? {
    guard x.isFinite, y.isFinite, x >= 0, y >= 0,
          x < bounds.width, y < bounds.height else { return nil }
    return CGPoint(x: bounds.minX + x, y: bounds.minY + y)
}
if CommandLine.arguments == [CommandLine.arguments[0], "--self-test"] {
    let box = CGRect(x: 753, y: 165, width: 414, height: 780)
    assert(point(200, 84, in: box) == CGPoint(x: 953, y: 249))
    assert(point(-1, 84, in: box) == nil)
    assert(point(414, 84, in: box) == nil)
    assert(point(Double.nan, 84, in: box) == nil)
    print("coordinate guards: PASS (no UI events)")
    exit(0)
}
let args = CommandLine.arguments
guard args.count == 4, let id = UInt32(args[1]),
      let x = Double(args[2]), let y = Double(args[3]),
      CGPreflightPostEventAccess() else { fatalError("Invalid arguments or missing event permission") }
func windows() -> [[String: Any]] {
    CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], 0) as? [[String: Any]] ?? []
}
guard let window = windows().first(where: { ($0[kCGWindowNumber as String] as? UInt32) == id }),
      window[kCGWindowOwnerName as String] as? String == "WeChat",
      window[kCGWindowName as String] as? String == "趋势动物Pro",
      let pid = window[kCGWindowOwnerPID as String] as? Int32,
      let app = NSRunningApplication(processIdentifier: pid),
      let dict = window[kCGWindowBounds as String] as? [String: Any],
      let bounds = CGRect(dictionaryRepresentation: dict as CFDictionary),
      let destination = point(x, y, in: bounds) else { fatalError("Observed target window mismatch") }
app.activate(options: [])
Thread.sleep(forTimeInterval: 0.3)
guard NSWorkspace.shared.frontmostApplication?.processIdentifier == pid,
      let top = windows().first(where: { ($0[kCGWindowLayer as String] as? Int) == 0 }),
      (top[kCGWindowNumber as String] as? UInt32) == id else { fatalError("Target window is not foreground") }
for type in [CGEventType.mouseMoved, .leftMouseDown, .leftMouseUp] {
    guard let event = CGEvent(mouseEventSource: nil, mouseType: type,
                              mouseCursorPosition: destination, mouseButton: .left) else { fatalError("Cannot create event") }
    event.post(tap: .cghidEventTap)
    Thread.sleep(forTimeInterval: 0.05)
}
print("One foreground click sent to window \(id), relative (\(x), \(y)); inspect screenshot for result")
