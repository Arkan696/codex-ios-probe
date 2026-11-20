import json
import os
import platform
import plistlib
import secrets
import shlex
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

SRC = Path(__file__).resolve().parent
BUNDLE = 'org.local.codexprobe'
MIN = '15.0'


def run(*argv, timeout=60):
    args = list(map(str, argv))
    print('+ ' + shlex.join(args), flush=True)
    with (out / 'runner.log').open('a') as log:
        print('+ ' + shlex.join(args), file=log, flush=True)
        with subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=log, start_new_session=True, text=True, errors='replace') as p:
            try:
                text, _ = p.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                print(f'{shlex.join(args)} timed out after {timeout}s', file=log, flush=True)
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError as cleanup:
                    print(f'Process-group cleanup: {cleanup}', file=log, flush=True)
                    try:
                        p.kill()
                    except OSError as direct:
                        print(f'Child cleanup: {direct}', file=log, flush=True)
                raise
            print(text, file=log)
            if p.returncode:
                raise RuntimeError(f'{args[0]} returned {p.returncode}; see {log.name}')
            return text.strip()


def sim(*args, **kwargs):
    return run('xcrun', 'simctl', *args, **kwargs)


def device():
    for attempt in (1, 2):
        try:
            inventory = json.loads(sim('list', '--json', timeout=180))
            break
        except subprocess.TimeoutExpired:
            with (out / 'runner.log').open('a') as log:
                print(f'simctl inventory attempt {attempt}/2 timed out after 180s', file=log)
            if attempt == 2:
                try:
                    run('/usr/bin/log', 'show', '--last', '7m', '--style', 'compact',
                        '--predicate', 'process == "com.apple.CoreSimulator.CoreSimulatorService"', timeout=30)
                except Exception as exc:
                    print('CoreSimulator diagnostics unavailable:', exc, file=sys.stderr)
                raise
    runtimes = [r for r in inventory['runtimes'] if r['isAvailable'] and
                '.SimRuntime.iOS-' in r['identifier'] and int(r['version'].split('.')[0]) >= 15]
    runtimes.sort(key=lambda r: tuple(map(int, r['version'].split('.'))), reverse=True)
    for runtime in runtimes:
        phones = [d for d in inventory['devices'].get(runtime['identifier'], [])
                  if d['isAvailable'] and '.iPhone-' in d.get('deviceTypeIdentifier', '')
                  and d['state'] in ('Booted', 'Shutdown', 'Booting')]
        if phones:
            return min(phones, key=lambda d: d['state'] != 'Booted')['udid'], False
    for runtime in runtimes:
        # Older simctl lists expose the compatibility range on each type instead.
        parts = (list(map(int, runtime['version'].split('.'))) + [0, 0])[:3]
        version = (parts[0] << 16) | (parts[1] << 8) | parts[2]
        compatible = [m for m in inventory.get('devicetypes', [])
                      if m.get('minRuntimeVersion', 0) <= version <= m.get('maxRuntimeVersion', 0xffffffff)]
        for model in runtime.get('supportedDeviceTypes') or compatible:
            if '.iPhone-' in model['identifier']:
                return sim('create', 'CodexProbe-' + nonce, model['identifier'], runtime['identifier']), True
    raise RuntimeError('Install an available iOS 15+ iPhone simulator runtime in Xcode')


def build(lib, flags, arch):
    sdk = run('xcrun', '--sdk', 'iphonesimulator', '--show-sdk-path')
    run('xcrun', 'lipo', lib, '-verify_arch', arch)
    bundle = out / 'Probe.app'
    bundle.mkdir()
    plist = dict(ProbeBuildNonce=nonce, CFBundleIdentifier=BUNDLE, CFBundleExecutable='Probe', CFBundleName='Probe',
                 CFBundleVersion='1', CFBundleShortVersionString='1.0', CFBundlePackageType='APPL',
                 CFBundleInfoDictionaryVersion='6.0', CFBundleSupportedPlatforms=['iPhoneSimulator'],
                 DTPlatformName='iphonesimulator', MinimumOSVersion=MIN, LSRequiresIPhoneOS=True,
                 UIDeviceFamily=[1], UILaunchScreen={},
                 UIApplicationSceneManifest={'UIApplicationSupportsMultipleScenes': False})
    (bundle / 'Info.plist').write_bytes(plistlib.dumps(plist))
    run('xcrun', '--sdk', 'iphonesimulator', 'swiftc', '-swift-version', '5', '-parse-as-library',
        '-target', f'{arch}-apple-ios{MIN}-simulator', '-sdk', sdk,
        '-import-objc-header', SRC / 'Probe.h', SRC / 'Probe.swift', lib, '-o', bundle / 'Probe',
        '-framework', 'UIKit', '-framework', 'Foundation', '-framework', 'CoreFoundation',
        '-framework', 'Security', '-framework', 'SystemConfiguration', '-lc++', '-lz', '-lsqlite3',
        '-liconv', '-lresolv', *flags, timeout=600)
    run('xcrun', 'vtool', '-show-build', bundle / 'Probe')
    run('/usr/bin/codesign', '--force', '--sign', '-', '--timestamp=none', bundle)
    run('/usr/bin/codesign', '--verify', '--strict', bundle)
    return bundle


def install_checked(udid, bundle):
    try:
        sim('install', udid, bundle, timeout=120)
    except subprocess.TimeoutExpired as original:
        try:
            path = Path(sim('get_app_container', udid, BUNDLE, 'app', timeout=60))
            if not path.is_absolute():
                raise ValueError('No absolute installed app path')
            with (path / 'Info.plist').open('rb') as stream:
                info = plistlib.load(stream)
            if info.get('CFBundleIdentifier') != BUNDLE or info.get('ProbeBuildNonce') != nonce:
                raise ValueError('Installed app is not from this run')
        except Exception as check:
            with (out / 'runner.log').open('a') as log:
                print(f'Install timeout; current build not confirmed: {check}', file=log)
            raise original from check
        with (out / 'runner.log').open('a') as log:
            print('Install timed out but current build nonce verified; continuing', file=log)


def validate(record, nonce):
    if record.get('runID') != nonce or type(record.get('code')) is not int or record['code'] != 0 or record.get('error') is not None:
        raise ValueError(f'Native probe did not succeed for this launch: {record!r}')
    text = record['uuid']
    parsed = uuid.UUID(text)
    if not parsed.int or str(parsed) != text.lower():
        raise ValueError('Expected canonical non-nil session UUID')
    digest = record.get('expectedSHA256', '')
    if record.get('byteCount') != 32 or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest) or digest != record.get('receivedSHA256'):
        raise ValueError('No matching 32-byte fs/readFile evidence')
    a = record.get('approval') or {}
    def is_digest(x):
        return isinstance(x, str) and len(x) == 64 and all(c in '0123456789abcdef' for c in x)
    old, new = a.get('beforeSHA256'), a.get('previewSHA256')
    if not (is_digest(old) and is_digest(new) and old != new
            and a.get('rejectedSHA256') == old and a.get('approvedSHA256') == new
            and type(a.get('rejectedWrites')) is int and a['rejectedWrites'] == 0
            and type(a.get('approvedWrites')) is int and a['approvedWrites'] == 1):
        raise ValueError('Missing approval evidence: reject must preserve original with zero writes; allow must write preview once')
    return text


def smoke(lib, flags):
    live = os.environ.get('LIVE_PROPOSAL') == '1'
    key = os.environ.pop('MODEL_API_KEY', '')  # do not inherit into Xcode, simctl or app
    if live and (not key or os.environ.get('MODEL_BASE_URL', '').rstrip('/') != 'https://new.maoyu.cfd'):
        raise ValueError('Live test requires the configured secret and approved HTTPS origin')
    attempted = False
    if platform.system() != 'Darwin':
        raise RuntimeError('Requires macOS, full Xcode and an installed iPhone simulator runtime')
    arch = os.getenv('SIM_ARCH', platform.machine())
    if arch not in ('arm64', 'x86_64') or arch != platform.machine():
        raise ValueError('SIM_ARCH must match the native macOS runner (arm64 or x86_64)')
    if lib.suffix != '.a' or not lib.is_file():
        raise ValueError(f'Missing simulator .a: {lib}')
    bundle = build(lib, flags, arch)
    udid, created = device()
    try:
        sim('bootstatus', udid, '-b', timeout=300)
        install_checked(udid, bundle)
        container = Path(sim('get_app_container', udid, BUNDLE, 'data'))
        if not container.is_absolute() or not container.is_dir():
            raise ValueError(f'Unexpected simulator data container: {container}')
        result = container / 'Documents/probe-result.json'
        try:
            extra = ['--live-proposal'] if live else []
            sim('launch', '--terminate-running-process', udid, BUNDLE, '--probe', '--run-id', nonce, *extra)
        except subprocess.TimeoutExpired:
            # The simulator may already have delivered the launch. Do not relaunch.
            with (out / 'runner.log').open('a') as log:
                print('simctl launch timed out; checking only this run ID for 300s', file=log)
        deadline = time.monotonic() + 300  # read regression plus both decision paths
        while time.monotonic() < deadline:
            if live and not attempted and (container / 'Documents/live-request.json').exists():
                from live_relay import service
                attempted = True  # single-use latch before any network I/O
                service(container, nonce, key)
                key = ''
            if result.exists():
                raw = result.read_bytes()  # App publishes by atomic rename.
                record = json.loads(raw)
                if record.get('runID') == nonce:
                    (out / 'result.json').write_bytes(raw)
                    if live:
                        import hashlib
                        proof = record.get('live') or {}
                        if not attempted or not proof or not proof.get('responseID') or not proof.get('model'):
                            raise ValueError(record.get('error') or 'No evidence of a real provider response')
                        wire = json.loads((container / 'Documents/live-response.json').read_bytes())
                        h = lambda s: hashlib.sha256(s.encode('utf-8')).hexdigest()
                        a = record.get('approval') or {}
                        if not (wire['runID'] == nonce and wire['model'] == proof['model'] and wire['responseID'] == proof['responseID'] and h(wire['candidate']) == proof['candidateSHA256'] == a.get('previewSHA256') and h(f'Meeting: Tuesday at 10:00\nReference: {nonce}') == proof['beforeApprovalSHA256'] == a.get('beforeSHA256')):
                            raise ValueError('Live model evidence does not match approved bytes')
                    print('Session UUID:', validate(record, nonce), flush=True)
                    return
            time.sleep(0.5)
        raise TimeoutError('No fresh Documents/probe-result.json within 300 seconds')
    finally:
        try:
            logs = sim('spawn', udid, 'log', 'show', '--last', '5m', '--style', 'compact',
                       '--predicate', 'process == "Probe" OR ((process == "SpringBoard" OR process == "runningboardd" OR process == "ReportCrash") AND eventMessage CONTAINS[c] "org.local.codexprobe")', timeout=30)
            (out / 'app.log').write_text(logs + '\n')
        except Exception as exc:
            print('Diagnostic log unavailable:', exc, file=sys.stderr)
        if created:
            sim('delete', udid)


if __name__ == '__main__':
    if len(sys.argv) < 2:
        sys.exit('Usage: [SIM_ARCH=arm64|x86_64] [PROBE_OUTPUT_DIR=DIR] run.sh SIMULATOR.a [swiftc link args ...]')
    out = Path(os.getenv('PROBE_OUTPUT_DIR', SRC / 'artifacts')).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if any((out / name).exists() for name in ('Probe.app', 'result.json', 'runner.log', 'failure.txt', 'app.log')):
        sys.exit('Use a fresh PROBE_OUTPUT_DIR for each invocation; prior artifacts will not be overwritten')
    nonce = secrets.token_hex(16)  # Correlation token only, never a session UUID.
    print('Artifacts:', out, flush=True)
    try:
        smoke(Path(sys.argv[1]).resolve(), sys.argv[2:])
    except Exception as exc:
        (out / 'failure.txt').write_text(str(exc) + '\n')
        sys.exit(str(exc))
