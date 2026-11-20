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
                os.killpg(p.pid, signal.SIGKILL)
                raise
            print(text, file=log)
            if p.returncode:
                raise RuntimeError(f'{args[0]} returned {p.returncode}; see {log.name}')
            return text.strip()


def sim(*args, **kwargs):
    return run('xcrun', 'simctl', *args, **kwargs)


def device():
    inventory = json.loads(sim('list', '--json'))
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
    plist = dict(CFBundleIdentifier=BUNDLE, CFBundleExecutable='Probe', CFBundleName='Probe',
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


def validate(record, nonce):
    if record.get('runID') != nonce or type(record.get('code')) is not int or record['code'] != 0 or record.get('error') is not None:
        raise ValueError(f'Native probe did not succeed for this launch: {record!r}')
    text = record['uuid']
    parsed = uuid.UUID(text)
    if not parsed.int or str(parsed) != text.lower():
        raise ValueError('Expected canonical non-nil session UUID')
    return text


def smoke(lib, flags):
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
        sim('install', udid, bundle, timeout=120)
        container = Path(sim('get_app_container', udid, BUNDLE, 'data'))
        if not container.is_absolute() or not container.is_dir():
            raise ValueError(f'Unexpected simulator data container: {container}')
        result = container / 'Documents/probe-result.json'
        sim('launch', '--terminate-running-process', udid, BUNDLE, '--probe', '--run-id', nonce)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if result.exists():
                raw = result.read_bytes()  # App publishes by atomic rename.
                record = json.loads(raw)
                if record.get('runID') == nonce:
                    (out / 'result.json').write_bytes(raw)
                    print('Session UUID:', validate(record, nonce), flush=True)
                    return
            time.sleep(0.5)
        raise TimeoutError('No fresh Documents/probe-result.json within 120 seconds')
    finally:
        try:
            logs = sim('spawn', udid, 'log', 'show', '--last', '5m', '--style', 'compact',
                       '--predicate', 'process == "Probe"', timeout=30)
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
