# Approval-gated write probe (simulator)

Goal: demonstrate that the app can offer a specific proposed edit and use the embedded official app-server to apply that edit only after the app has an affirmative decision. This is **app-level** approval, not Codex command approval, a paid model turn, a physical-device test, or proof that a human tapped the controls in CI.

- Scope: a fresh test directory and fixed `review.txt`. No user project selection, credentials, network model call, or shell tool.
- Manual: show the original and proposed UTF-8 text, and offer an explicit reject or allow decision. Only the approved bytes shown in the preview may be submitted.
- CI: exercise the decision handler with both decisions for the same test file. Keep the existing 32-byte read regression. If the original file no longer matches the preview's base, do not silently approve an outdated edit.
- Reject: no `fs/writeFile`; file readback is byte-identical to the original. Allow: exactly one successful `fs/writeFile`; file readback is byte-identical to the proposed text. A Swift filesystem read independently checks the engine's readback.
- Evidence: fresh `runID`, valid session UUID, two distinct SHA-256 values for original and proposal; post-reject equals original; post-allow equals proposal. The runner must reject old, incomplete or contradictory evidence. A signed app or successful build alone is not a pass.
