// A disposable unactivated AppKit process for the opt-in AX reference E2E.
import AppKit

let arguments = CommandLine.arguments
guard arguments.count == 2 else { fatalError("Expected marker path") }

let app = NSApplication.shared
app.setActivationPolicy(.accessory)

let window = NSWindow(
    contentRect: NSRect(x: 40, y: 40, width: 320, height: 150),
    styleMask: [.titled], backing: .buffered, defer: false
)
window.title = "Sartel T1 AX fixture"

class Handler: NSObject {
    var path: String = ""
    var button: NSButton?

    @objc func pressed() {
        button?.title = "Pressed"
        try? "pressed".write(toFile: path, atomically: true, encoding: .utf8)
    }
}

let handler = Handler()
handler.path = arguments[1]
let button = NSButton(title: "Write marker", target: handler, action: #selector(Handler.pressed))
button.frame = NSRect(x: 20, y: 78, width: 180, height: 36)
handler.button = button
window.contentView?.addSubview(button)

let field = NSTextField(frame: NSRect(x: 20, y: 25, width: 220, height: 30))
field.stringValue = "Empty"
field.placeholderString = "Name"
field.setAccessibilityLabel("Name")
window.contentView?.addSubview(field)

window.orderBack(nil)
Timer.scheduledTimer(withTimeInterval: 120, repeats: false) { _ in app.terminate(nil) }
app.run()
