#!/usr/bin/env python3
"""Unattended Fusion Bulk Export on macOS or Windows.

  autorun.py inventory OUTPUT                       list everything, open nothing
  autorun.py export OUTPUT [--project ID ...] [--limit N]
                                                    export (rerun the same command to resume)
  autorun.py status OUTPUT                          counts so far (no design names)
  autorun.py stop OUTPUT                            pause an export between designs

Sign in to Fusion first. The first run needs Fusion closed (or the add-in started by
hand from Utilities > Add-Ins); later runs reuse the open, watching Fusion session.
Environment: FUSION_APP (app name, .app or .exe path), AUTORUN_TIMEOUT_SECONDS
(default 86400).
"""
import argparse
import glob
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time

# Files the add-in creates at run time. Never copied over an installation (and never
# a broad glob: "autorun*" would also skip the autorun.py module itself).
RUNTIME_FILES = shutil.ignore_patterns(
    '__pycache__', 'selftest.json', 'selftest.json.last', 'selftest-output', 'autorun.json',
    'autorun.json.last', 'autorun.watch', 'autorun.alive', 'autorun-output', 'startup-error.txt')

REPO = Path(__file__).resolve().parent.parent
ALIVE_SECONDS = 20
SAFE_LOG = re.compile(r'^\S+ (design \d+/\d+|autorun (start (export|inventory|diagnostics)|finished)|inventory: [\d ,a-z]+)$')
MANUAL_HINT_SECONDS = 180
MANUAL_HINT = ('Fusion has not started the add-in. In Fusion: Utilities > Add-Ins > Scripts and '
               'Add-Ins, select FusionBulkExport, click Run (tick Run on Startup).')


def system():
    name = platform.system()
    if name not in ('Darwin', 'Windows'):
        raise SystemExit('Fusion runs only on macOS and Windows.')
    return name


def addins_dir(name=None, environ=None):
    name = name or system()
    environ = os.environ if environ is None else environ
    if name == 'Windows':
        base = Path(environ['APPDATA'])
    else:
        base = Path(environ.get('HOME', str(Path.home()))) / 'Library' / 'Application Support'
    return base / 'Autodesk' / 'Autodesk Fusion 360' / 'API' / 'AddIns'


def windows_launcher(environ=None):
    """Newest FusionLauncher.exe (else Fusion360.exe) under the webdeploy folders."""
    environ = os.environ if environ is None else environ
    production = Path(environ['LOCALAPPDATA']) / 'Autodesk' / 'webdeploy' / 'production'
    for exe in ('FusionLauncher.exe', 'Fusion360.exe'):
        found = [Path(p) for p in glob.glob(str(production / '*' / exe))]
        if found:
            return max(found, key=lambda p: p.stat().st_mtime)
    return None


def fusion_running(name=None):
    name = name or system()
    if name == 'Windows':
        out = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq Fusion360.exe', '/NH'],
                             capture_output=True, text=True).stdout
        return 'Fusion360.exe' in out
    return subprocess.run(['pgrep', '-x', 'Autodesk Fusion'], capture_output=True).returncode == 0


def launch_fusion(name=None):
    name = name or system()
    app = os.environ.get('FUSION_APP')
    if name == 'Windows':
        exe = app or windows_launcher()
        if not exe:
            raise SystemExit('Fusion not found; set FUSION_APP to FusionLauncher.exe.')
        subprocess.Popen([str(exe)], close_fds=True)
    else:
        subprocess.run(['open', '-a', app or 'Autodesk Fusion'], check=True)


def alive(installed, now=None):
    try:
        age = (time.time() if now is None else now) - (installed / 'autorun.alive').stat().st_mtime
    except OSError:
        return False
    return age < ALIVE_SECONDS


def set_startup(installed, on):
    manifest = installed / 'FusionBulkExport.manifest'
    settings = json.loads(manifest.read_text(encoding='utf-8'))
    settings['runOnStartup'] = bool(on)
    manifest.write_text(json.dumps(settings, indent=2) + '\n', encoding='utf-8')


def check_output(output):
    output = Path(output).expanduser().resolve()
    if len(str(output).encode('utf-16-le')) // 2 > 100:
        raise SystemExit('Use an output path of at most 100 characters.')
    return output


def install(installed, output, mode, projects=None, limit=None):
    output.mkdir(parents=True, exist_ok=True)
    for stale in ('autorun-result.json', 'STOP'):
        try:
            (output / stale).unlink()
        except FileNotFoundError:
            pass
    shutil.copytree(REPO / 'FusionBulkExport', installed, dirs_exist_ok=True,
                    ignore=RUNTIME_FILES)
    for stale in ('startup-error.txt', 'autorun-output/autorun-result.json'):
        try:
            (installed / stale).unlink()
        except FileNotFoundError:
            pass
    (installed / 'autorun.watch').write_text('', encoding='utf-8')
    config = dict(output=str(output), mode=mode)
    if projects:
        config['projects'] = projects
    if limit:
        config['limit'] = limit
    (installed / 'autorun.json').write_text(json.dumps(config, indent=2) + '\n', encoding='utf-8')


def summary(output):
    """Counts only: manifests and logs hold design names, which stay private."""
    lines = []
    result = output / 'autorun-result.json'
    if result.is_file():
        report = json.loads(result.read_text(encoding='utf-8'))
        lines.append('Result: mode %s, finished %s, Fusion %s' % (
            report.get('mode'), report.get('finished'), report.get('fusion_version')))
        lines.append('Counts: ' + json.dumps(report.get('counts', {})))
        for key in ('found', 'cancelled', 'discovery_errors', 'bytes'):
            if key in report:
                lines.append('%s: %s' % (key, report[key]))
        lines.append('Errors: %d' % len(report.get('errors', [])))
    manifest = output / 'manifest.json'
    if manifest.is_file():
        entries = json.loads(manifest.read_text(encoding='utf-8')).get('entries', [])
        statuses = {}
        for entry in entries:
            statuses[entry.get('status')] = statuses.get(entry.get('status'), 0) + 1
        size = sum(f.get('bytes', 0) for e in entries if e.get('status') == 'done' for f in e.get('files', []))
        lines.append('Manifest: %d entries %s, %d bytes' % (len(entries), json.dumps(statuses, sort_keys=True), size))
    log = output / 'autorun.log'
    if log.is_file():
        # Only known counter lines: error lines can carry project or design names.
        safe = [l for l in log.read_text(encoding='utf-8').splitlines() if SAFE_LOG.match(l)]
        if safe:
            lines.append('Last progress: ' + safe[-1])
    if (output / 'STOP').exists():
        lines.append('STOP is set; the export pauses between designs.')
    return lines or ['Nothing in ' + str(output) + ' yet.']


def wait(installed, output, timeout, hint_after=MANUAL_HINT_SECONDS, sleep=time.sleep, clock=time.monotonic):
    result = output / 'autorun-result.json'
    print('Waiting for ' + str(result) + ' (progress: autorun.log)', flush=True)
    start = clock()
    started_at = time.time() - 5
    hinted = False
    while not result.is_file():
        elapsed = clock() - start
        if elapsed >= timeout:
            raise SystemExit('Timed out. Inspect ' + str(output / 'autorun.log'))
        fallback = installed / 'autorun-output' / 'autorun-result.json'
        if fallback.is_file() and fallback.stat().st_mtime >= started_at:
            # The add-in could not use this job's configuration; its report went beside it.
            report = json.loads(fallback.read_text(encoding='utf-8'))
            print('The add-in rejected the job:', flush=True)
            for error in report.get('errors', []):
                print('  ' + error.get('error', ''), flush=True)
            return 1
        crash = installed / 'startup-error.txt'
        if crash.is_file():
            print('The add-in failed to start:\n' + crash.read_text(encoding='utf-8'), flush=True)
            return 1
        if not hinted and elapsed >= hint_after and not (installed / 'autorun.alive').exists():
            print(MANUAL_HINT, flush=True)
            hinted = True
        sleep(5)
    report = json.loads(result.read_text(encoding='utf-8'))
    for line in summary(output):
        print(line)
    return 1 if any(e.get('stage') == 'autorun' for e in report.get('errors', [])) else 0


def start(args):
    name = system()
    output = check_output(args.output)
    installed = addins_dir(name) / 'FusionBulkExport'
    watching = alive(installed)
    install(installed, output, args.command, getattr(args, 'project', None), getattr(args, 'limit', None))
    if watching:
        return wait(installed, output, args.timeout)
    if fusion_running(name):
        # Open but not watching: the job is in place; starting the add-in by hand picks it up.
        print(MANUAL_HINT, flush=True)
        return wait(installed, output, args.timeout, hint_after=float('inf'))
    set_startup(installed, True)
    try:
        launch_fusion(name)
        return wait(installed, output, args.timeout)
    finally:
        set_startup(installed, False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('inventory', 'export', 'diagnostics', 'status', 'stop'):
        p = sub.add_parser(command)
        p.add_argument('output')
        if command == 'export':
            p.add_argument('--project', action='append', help='project id from inventory.json (repeatable)')
            p.add_argument('--limit', type=int, help='export at most this many designs')
    args = parser.parse_args(argv)
    args.timeout = float(os.environ.get('AUTORUN_TIMEOUT_SECONDS', 86400))
    if args.command == 'status':
        for line in summary(check_output(args.output)):
            print(line)
        return 0
    if args.command == 'stop':
        output = check_output(args.output)
        output.mkdir(parents=True, exist_ok=True)
        (output / 'STOP').write_text('', encoding='utf-8')
        print('STOP set; the export pauses after the current design. Run export again to resume.')
        return 0
    if getattr(args, 'limit', None) is not None and args.limit < 1:
        parser.error('--limit must be positive')
    return start(args)


if __name__ == '__main__':
    sys.exit(main())
