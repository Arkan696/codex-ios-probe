import Foundation
import UIKit
import CryptoKit

private struct Approval: Codable {
    var beforeSHA256, previewSHA256, rejectedSHA256, approvedSHA256: String?
    var rejectedWrites, approvedWrites: Int?
}

private struct ResultRecord: Codable {
    let runID: String
    var code: Int32 = 0
    var uuid, error: String?
    var byteCount: Int?
    var expectedSHA256, receivedSHA256: String?
    var approval = Approval()
}

private struct Failure: LocalizedError {
    let errorDescription: String?
    let code: Int32
    init(_ message: String, _ code: Int32 = 1) { self.errorDescription = message; self.code = code }
}

private func digest(_ data: Data) -> String {
    SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
}

private func documents() throws -> URL {
    try FileManager.default.url(for: .documentDirectory, in: .userDomainMask, appropriateFor: nil, create: true)
}

private func freshHome() throws -> URL {
    let url = try documents().appendingPathComponent("home-\(ProcessInfo.processInfo.globallyUniqueString)", isDirectory: true)
    try FileManager.default.createDirectory(at: url, withIntermediateDirectories: false)
    return url
}

private struct Reply {
    let uuid: String
    let bytes: Data
    let writes: Int?
}

private func native(_ call: (UnsafeMutablePointer<CChar>, Int) -> Int32) throws -> Reply {
    var buffer = [CChar](repeating: -1, count: 4096)
    let code = buffer.withUnsafeMutableBufferPointer { call($0.baseAddress!, $0.count) }
    let output = buffer.firstIndex(of: 0).map { Data(buffer[..<$0].map { UInt8(bitPattern: $0) }) }
    guard code == 0 else {
        throw Failure(output.flatMap { String(data: $0, encoding: .utf8) } ?? "Native probe failed", code)
    }
    struct Wire: Decodable { let uuid, base64: String; let writes: Int? }
    guard let data = output, let wire = try? JSONDecoder().decode(Wire.self, from: data),
          let id = UUID(uuidString: wire.uuid), id.uuidString.lowercased() == wire.uuid.lowercased(),
          id.uuidString != "00000000-0000-0000-0000-000000000000", let bytes = Data(base64Encoded: wire.base64)
    else { throw Failure("Invalid native session or readback") }
    return Reply(uuid: id.uuidString.lowercased(), bytes: bytes, writes: wire.writes)
}

private let documentName = "review.txt"
private struct Proposal {
    let home: URL
    let before, after: Data
}

// approved == nil is read-only, not an empty write. Check native bytes against a separate Swift read.
private func edit(_ home: URL, approved: Data? = nil) throws -> Reply {
    let reply = try home.path.withCString { path in
        try native { out, cap in
            if let bytes = approved {
                return bytes.base64EncodedString().withCString { codex_edit_probe(path, $0, out, cap) }
            }
            return codex_edit_probe(path, nil, out, cap)
        }
    }
    guard reply.writes == (approved == nil ? 0 : 1) else { throw Failure("Unexpected native write count") }
    let disk = try Data(contentsOf: home.appendingPathComponent(documentName))
    guard reply.bytes == disk else { throw Failure("Native and Swift disk reads disagree") }
    return reply
}

// Setup is explicit in manual mode. This seeds only a newly created demo home; it never applies the edit.
private func makeReviewProposal() throws -> Proposal {
    let home = try freshHome()
    let original = Data("Meeting: Tuesday at 10:00".utf8)
    let proposed = Data("Meeting: Thursday at 14:00".utf8)
    try original.write(to: home.appendingPathComponent(documentName), options: .withoutOverwriting)
    let before = try edit(home).bytes
    guard before == original, before != proposed else { throw Failure("Wrong initial document") }
    return Proposal(home: home, before: before, after: proposed)
}

private enum Decision { case reject, allow }

// The controller serializes actions and consumes its pending proposal before dispatching.
// The ABI has no compare-and-swap: this preflight assumes no concurrent writer to the private demo home.
private func decide(_ choice: Decision, on proposal: Proposal) throws -> Reply {
    let current = try edit(proposal.home)
    guard current.bytes == proposal.before else { throw Failure("Document changed since preview. No edit sent.") }
    switch choice {
    case .reject: return current
    case .allow:
        let result = try edit(proposal.home, approved: proposal.after)
        guard result.bytes == proposal.after else { throw Failure("Readback differs from the approved preview") }
        return result
    }
}

private func performProbe(runID: String) throws -> ResultRecord {
    let destination = try documents().appendingPathComponent("probe-result.json")
    var r = ResultRecord(runID: runID)
    do {
        let home = try freshHome()
        var rng = SystemRandomNumberGenerator()
        let expected = Data((0..<32).map { _ in UInt8.random(in: 0...255, using: &rng) })
        r.expectedSHA256 = digest(expected)
        let file = home.appendingPathComponent("challenge.bin")
        try expected.write(to: file, options: .atomic)
        let reply = try home.path.withCString { path in try native { codex_probe(path, $0, $1) } }
        r.uuid = reply.uuid
        r.byteCount = reply.bytes.count
        r.receivedSHA256 = digest(reply.bytes)
        guard reply.bytes == expected, try Data(contentsOf: file) == expected else { throw Failure("32-byte readback mismatch") }

        let p = try makeReviewProposal()
        r.approval.beforeSHA256 = digest(p.before)
        r.approval.previewSHA256 = digest(p.after)
        let rejected = try decide(.reject, on: p)
        r.approval.rejectedSHA256 = digest(rejected.bytes)
        r.approval.rejectedWrites = rejected.writes
        let approved = try decide(.allow, on: p)
        r.approval.approvedSHA256 = digest(approved.bytes)
        r.approval.approvedWrites = approved.writes
    } catch {
        r.code = (error as? Failure)?.code ?? 1
        r.error = error.localizedDescription
    }
    // smoke.py copies this atomic, run-ID-correlated record to result.json before checking the code.
    try JSONEncoder().encode(r).write(to: destination, options: .atomic)
    return r
}

final class ProbeView: UIViewController {
    private let status = UILabel()
    private let preview = UILabel()
    private let create = UIButton(type: .system)
    private let reject = UIButton(type: .system)
    private let allow = UIButton(type: .system)
    private let worker = DispatchQueue(label: "app.probe.worker", qos: .userInitiated)
    private var pending: Proposal?

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground
        status.text = "Review an edit to review.txt"
        status.font = .preferredFont(forTextStyle: .headline)
        status.numberOfLines = 0
        preview.numberOfLines = 0
        preview.font = .preferredFont(forTextStyle: .body)
        preview.text = "Create a sample document to see the proposed change."
        create.setTitle("Create sample & preview edit", for: .normal)
        reject.setTitle("Reject — keep original", for: .normal)
        allow.setTitle("Allow — apply this edit", for: .normal)
        create.addTarget(self, action: #selector(loadPreview), for: .touchUpInside)
        reject.addTarget(self, action: #selector(tapReject), for: .touchUpInside)
        allow.addTarget(self, action: #selector(tapAllow), for: .touchUpInside)
        reject.isEnabled = false
        allow.isEnabled = false
        let stack = UIStackView(arrangedSubviews: [status, preview, create, reject, allow])
        stack.axis = .vertical
        stack.spacing = 12
        stack.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(stack)
        NSLayoutConstraint.activate([stack.centerYAnchor.constraint(equalTo: view.safeAreaLayoutGuide.centerYAnchor),
            stack.leadingAnchor.constraint(equalTo: view.layoutMarginsGuide.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: view.layoutMarginsGuide.trailingAnchor)])

        let args = ProcessInfo.processInfo.arguments
        if args.contains("--probe") {
            [preview, create, reject, allow].forEach { $0.isHidden = true }
            let runID = args.firstIndex(of: "--run-id").flatMap { args.dropFirst($0 + 1).first } ?? "manual"
            status.text = "Running isolated probes…"
            work({ try performProbe(runID: runID) }) { r in
                self.status.text = r.code == 0 ? r.uuid : r.error
            }
        }
    }

    private func work<T>(_ job: @escaping () throws -> T, _ done: @escaping (T) -> Void) {
        worker.async {
            do {
                let value = try job()
                DispatchQueue.main.async { done(value) }
            } catch {
                DispatchQueue.main.async {
                    self.status.text = error.localizedDescription
                    self.create.isEnabled = true
                }
            }
        }
    }

    @objc private func loadPreview() {
        guard create.isEnabled, pending == nil else { return }
        create.isEnabled = false
        status.text = "Reading sample…"
        preview.text = nil
        work(makeReviewProposal) { p in
            self.pending = p
            self.preview.text = "Before\n\(String(decoding: p.before, as: UTF8.self))\n\nAfter (proposed)\n\(String(decoding: p.after, as: UTF8.self))"
            self.status.text = "Review the change to review.txt"
            self.reject.isEnabled = true
            self.allow.isEnabled = true
        }
    }

    @objc private func tapReject() { submit(.reject) }
    @objc private func tapAllow() { submit(.allow) }
    private func submit(_ choice: Decision) {
        guard let p = pending else { return }
        pending = nil
        reject.isEnabled = false
        allow.isEnabled = false
        status.text = "Checking review.txt…"
        work({ try decide(choice, on: p) }) { _ in
            self.status.text = choice == .reject ? "Rejected. Original verified unchanged." : "Allowed. Saved bytes match the preview."
            self.create.isEnabled = true
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
