# iPhone simulator smoke test

Run on a native macOS runner with full Xcode selected, an installed iOS 15+ simulator runtime, and Python 3.8+. Example (bash):

```bash
extra=(-Xlinker -framework -Xlinker Network -Xlinker -lz)
PROBE_OUTPUT_DIR="$RUNNER_TEMP/probe-output" SIM_ARCH="$(uname -m)" \
  simulator/app/run.sh "$SIMULATOR_STATICLIB" "${extra[@]}"
```

Argument 1 must be the bridge `.a` exporting `codex_probe`; all remaining arguments are appended verbatim to `swiftc`, preserving order and spaces. CI can convert Cargo's `native-static-libs` tokens to separate `-Xlinker TOKEN` pairs. No token, API call, provisioning profile or developer signing identity is needed. `SIM_ARCH` defaults to the host architecture. Use **`aarch64-apple-ios-sim`** on arm64 or **`x86_64-apple-ios`** on Intel for Rust; `aarch64-apple-ios` is for physical devices and is not interchangeable. The Swift triple is `<arch>-apple-ios15.0-simulator`, SDK `iphonesimulator`. UIKit, Foundation, CoreFoundation, Security, SystemConfiguration and common native libraries are included before the extra flags.

The runner generates an iPhone-only simulator Info.plist, ad-hoc signs and verifies `Probe.app`, selects an available iPhone by UDID (creates a compatible iPhone when needed), boots it and launches with `--probe --run-id NONCE`. A run without `--probe` displays the native Run button. The native call runs off the main thread with a fresh home beneath Documents. The app atomically writes `Documents/probe-result.json` and displays the UUID or diagnostic. The runner accepts only a matching nonce, integer return code zero and a canonical non-nil UUID from the C buffer. It never generates a substitute UUID. Serialize jobs sharing a simulator/device set. Only devices created by the runner are deleted.

Use a fresh `PROBE_OUTPUT_DIR` (default: `simulator/app/artifacts`). Collect `runner.log`, `app.log` when available, `result.json` once a matching result is returned (including native failures), and `failure.txt` on runner failure. The generated `.app` is there too. Build timeout: 600 s; boot: 300 s; install and result wait: 120 s each; other commands: at most 60 s. Exit status, not mere file existence, determines success.
