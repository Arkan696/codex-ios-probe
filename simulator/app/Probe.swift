import Foundation
import UIKit

private struct ResultRecord: Codable {
    let runID: String
    let code: Int32
    let uuid: String?
    let error: String?
}

// This is deliberately outside the UI types: all filesystem and native work
// happens on the worker queue. The only source of a session UUID is the C ABI.
private func performProbe(runID: String) throws -> ResultRecord {
    let documents = try FileManager.default.url(for: .documentDirectory, in: .userDomainMask,
                                                appropriateFor: nil, create: true)
    let home = documents.appendingPathComponent("home-\(ProcessInfo.processInfo.globallyUniqueString)", isDirectory: true)
    try FileManager.default.createDirectory(at: home, withIntermediateDirectories: false)
    var bytes = [CChar](repeating: -1, count: 4096)
    let code = home.path.withCString { path in
        bytes.withUnsafeMutableBufferPointer { codex_probe(path, $0.baseAddress!, $0.count) }
    }
    let text = bytes.firstIndex(of: 0).flatMap { end in
        String(bytes: bytes[..<end].map { UInt8(bitPattern: $0) }, encoding: .utf8)
    }
    let id = text.flatMap(UUID.init(uuidString:))
    let valid = code == 0 && id != nil && text?.utf8.count == 36 &&
        text?.lowercased() == id?.uuidString.lowercased() &&
        text != "00000000-0000-0000-0000-000000000000"
    let record = ResultRecord(runID: runID, code: code, uuid: valid ? text : nil,
                              error: valid ? nil : (code == 0 ? "Invalid UUID from probe" : (text ?? "Native probe failed")))
    try JSONEncoder().encode(record).write(to: documents.appendingPathComponent("probe-result.json"), options: .atomic)
    return record
}

final class ProbeView: UIViewController {
    private let status = UILabel()
    private let button = UIButton(type: .system)

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground
        status.text = "Ready"
        status.numberOfLines = 0
        status.textAlignment = .center
        button.setTitle("Run session probe", for: .normal)
        button.addTarget(self, action: #selector(start), for: .touchUpInside)
        let stack = UIStackView(arrangedSubviews: [status, button])
        stack.axis = .vertical
        stack.spacing = 16
        stack.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(stack)
        NSLayoutConstraint.activate([stack.centerYAnchor.constraint(equalTo: view.centerYAnchor),
            stack.leadingAnchor.constraint(equalTo: view.layoutMarginsGuide.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: view.layoutMarginsGuide.trailingAnchor)])
        if ProcessInfo.processInfo.arguments.contains("--probe") { start() }
    }

    @objc private func start() {
        guard button.isEnabled else { return }
        button.isEnabled = false
        status.text = "Running…"
        let args = ProcessInfo.processInfo.arguments
        let runID = args.firstIndex(of: "--run-id").flatMap { args.dropFirst($0 + 1).first } ?? "manual"
        DispatchQueue.global(qos: .userInitiated).async {
            let message: String
            do {
                let r = try performProbe(runID: runID)
                message = r.uuid ?? "Probe failed (\(r.code)): \(r.error ?? "unknown error")"
            } catch { message = error.localizedDescription }
            NSLog("Probe: %@", message)
            DispatchQueue.main.async {
                self.status.text = message
                self.button.isEnabled = true
            }
        }
    }
}

final class SceneDelegate: UIResponder, UIWindowSceneDelegate {
    var window: UIWindow?
    func scene(_ scene: UIScene, willConnectTo session: UISceneSession,
               options connectionOptions: UIScene.ConnectionOptions) {
        guard let scene = scene as? UIWindowScene else { return }
        let window = UIWindow(windowScene: scene)
        self.window = window
        window.rootViewController = ProbeView()
        window.makeKeyAndVisible()
    }
}

@main final class AppDelegate: UIResponder, UIApplicationDelegate {
    func application(_ application: UIApplication, configurationForConnecting session: UISceneSession,
                     options: UIScene.ConnectionOptions) -> UISceneConfiguration {
        let config = UISceneConfiguration(name: nil, sessionRole: session.role)
        config.delegateClass = SceneDelegate.self
        return config
    }
}
