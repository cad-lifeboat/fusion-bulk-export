#!/usr/bin/env python3
"""Unattended Fusion Bulk Export on macOS or Windows.

  autorun.py inventory OUTPUT                       list everything, open nothing
  autorun.py export OUTPUT [--project ID ...] [--folder ID:Folder/Sub ...]
                    [--folder ID:id=FOLDER_ID ...] [--limit N] [--designs-only]
                                                    export (rerun the same command to resume);
                                                    other files (PDFs, drawings, uploads) are
                                                    saved too unless --designs-only
  autorun.py status OUTPUT                          counts so far (no design names)
  autorun.py check OUTPUT                           check every exported file again (full hashes,
                                                    archives, STEP files); needs no Fusion
  autorun.py stop OUTPUT                            pause an export between designs

Sign in to Fusion first. The first run needs Fusion closed (or the add-in started by
hand from Utilities > Add-Ins); later runs reuse the open Fusion session, where the add-in
always watches for jobs and its toolbar button stays available. A job sent while an export
from the button is running is refused with a "busy" message: run the command again later.
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
    'autorun.json.last', 'autorun.watch', 'autorun.alive', 'autorun-output', 'startup-error.txt',
    'errors.log', 'trace', 'trace.log')

REPO = Path(__file__).resolve().parent.parent
ALIVE_SECONDS = 20
SAFE_LOG = re.compile(r'^\S+ (project \d+/\d+ design \d+|autorun (start (export|inventory|diagnostics)|finished|busy)|inventory: [\d ,a-z]+|inventory hub \d+/\d+ project \d+/\d+: \d+ files|inventory stopped)$')
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
    """The output folder as an absolute path. Its length is not limited: the add-in writes
    long paths itself (and says so if a folder cannot be used)."""
    return Path(output).expanduser().resolve()


def folder_map(values):
    """--folder values as {project id: [paths or {"id": folder id}]}: ID:Path/To/Folder by
    names, or ID:id=FOLDER_ID by the folder's id (from inventory.json's folder_ids), which also
    works for a folder whose name holds "/"."""
    folders = {}
    for value in values or []:
        project, sep, path = value.partition(':')
        if path.startswith('id='):
            if not sep or not project or not path[3:]:
                raise SystemExit('--folder needs PROJECT_ID:id=FOLDER_ID, got ' + repr(value))
            folders.setdefault(project, []).append(dict(id=path[3:]))
            continue
        if not sep or not project or not path.strip('/'):
            raise SystemExit('--folder needs PROJECT_ID:Folder/Subfolder or PROJECT_ID:id=FOLDER_ID, got '
                             + repr(value))
        folders.setdefault(project, []).append(path.strip('/'))
    return folders or None


def install(installed, output, mode, projects=None, limit=None, folders=None, designs_only=False):
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
    try:
        (installed / 'autorun.watch').unlink()  # the marker of earlier versions; no longer used
    except FileNotFoundError:
        pass
    config = dict(output=str(output), mode=mode)
    if projects:
        config['projects'] = projects
    if folders:
        config['folders'] = folders
    if limit:
        config['limit'] = limit
    if designs_only:
        config['designs_only'] = True
    (installed / 'autorun.json').write_text(json.dumps(config, indent=2) + '\n', encoding='utf-8')


def count_line(counts):
    """The run's counts in words; results of versions before 0.3 had done/failed/skipped."""
    if 'exported' in counts:
        return ('Counts: %d exported now, %d already up to date, %d other files saved, %d skipped, %d failed'
                % tuple(counts.get(key, 0) for key in ('exported', 'up_to_date', 'files', 'skipped', 'failed')))
    return 'Counts: ' + json.dumps(counts, sort_keys=True)


def summary(output):
    """Counts only: manifests and logs hold design names, which stay private."""
    lines = []
    result = output / 'autorun-result.json'
    if result.is_file():
        report = json.loads(result.read_text(encoding='utf-8'))
        lines.append('Result: mode %s, finished %s, Fusion %s' % (
            report.get('mode'), report.get('finished'), report.get('fusion_version')))
        lines.append(count_line(report.get('counts', {})))
        if report.get('stopped_reason'):
            lines.append('Stopped by itself (%s); run export again to carry on.' % report.get('stopped_code'))
        if report.get('low_space'):
            lines.append('Low disk space: %d MB free' % ((report.get('free_bytes') or 0) >> 20))
        for key in ('found', 'cancelled', 'discovery_errors', 'bytes'):
            if key in report:
                lines.append('%s: %s' % (key, report[key]))
        lines.append('Errors: %d' % len(report.get('errors', [])))
    manifest = output / 'manifest.json'
    if manifest.is_file():
        entries = json.loads(manifest.read_text(encoding='utf-8')).get('entries', [])
        size = sum(f.get('bytes', 0) for e in entries if e.get('status') == 'done' for f in e.get('files', []))
        for label, kind in (('Manifest', None), ('Designs', 'design'), ('Other files', 'file')):
            chosen = [e for e in entries if kind is None or (e.get('kind') or 'design') == kind]
            if kind is not None and not chosen:
                continue
            statuses = {}
            for entry in chosen:
                statuses[entry.get('status')] = statuses.get(entry.get('status'), 0) + 1
            lines.append('%s: %d entries %s%s' % (label, len(chosen), json.dumps(statuses, sort_keys=True),
                                                  ', %d bytes' % size if kind is None else ''))
        warned = sum(1 for e in entries if e.get('status') == 'done' and e.get('warnings'))
        if warned:
            lines.append('Warnings: %d item(s) exported with a warning (see the report)' % warned)
    log = output / 'autorun.log'
    if log.is_file():
        # Only known counter lines: error lines can carry project or design names.
        safe = [l for l in log.read_text(encoding='utf-8').splitlines() if SAFE_LOG.match(l)]
        if safe:
            lines.append('Last progress: ' + safe[-1])
    if (output / 'index.html').is_file():
        lines.append('Report: ' + str(output / 'index.html'))
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
            print('Fusion is busy:' if report.get('busy') else 'The add-in rejected the job:', flush=True)
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
    if report.get('busy'):
        for error in report.get('errors', []):
            print('Fusion is busy: ' + error.get('error', ''), flush=True)
        return 1
    for line in summary(output):
        print(line)
    return 1 if any(e.get('stage') == 'autorun' for e in report.get('errors', [])) else 0


def start(args):
    name = system()
    output = check_output(args.output)
    installed = addins_dir(name) / 'FusionBulkExport'
    watching = alive(installed)
    install(installed, output, args.command, getattr(args, 'project', None), getattr(args, 'limit', None),
            folder_map(getattr(args, 'folder', None)), getattr(args, 'designs_only', False))
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


def reset(installed):
    """Kept for old instructions: clears stale job files. The add-in no longer has an unattended
    mode; its toolbar button is always there. A running job finishes first."""
    removed = []
    for name in ('autorun.json', 'autorun.watch'):
        try:
            (installed / name).unlink()
            removed.append(name)
        except FileNotFoundError:
            pass
    set_startup(installed, False) if (installed / 'FusionBulkExport.manifest').is_file() else None
    print('Nothing to reset: the Fusion Bulk Export button is always available%s.'
          % (' (removed stale ' + ', '.join(removed) + ')' if removed else ''))
    return 0


def check(output, names=False):
    """Check this folder: every exported file again, without Fusion. Counts only unless names."""
    package = REPO / 'FusionBulkExport'
    if str(package) not in sys.path:
        sys.path.insert(0, str(package))
    from fusion_bulk_export import checks
    if not (output / 'manifest.json').is_file():
        print('Nothing to check: no manifest.json in ' + str(output))
        return 1

    def show(number, entry, line, problems, notes):
        if not (problems or notes):
            return
        label = entry.get('name') if names else ('file %d' if entry.get('kind') == 'file' else 'design %d') % number
        for problem in problems:
            print('%s: PROBLEM %s' % (label, problem))
        for note in notes:
            print('%s: NOTE %s' % (label, note))
    totals = checks.check_folder(output, show=show)
    print('Checked %d finished item(s): %d with a problem, %d missing file(s), %d changed file(s), '
          '%d STEP solid count(s) different, %d not exported yet (failed).'
          % (totals['done'], totals['failed_checks'] - totals['failed'], totals['missing'], totals['changed'],
             totals['solids_differ'], totals['failed']))
    print(json.dumps(totals))
    return 0 if not totals['failed_checks'] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('inventory', 'export', 'diagnostics', 'status', 'stop', 'check'):
        p = sub.add_parser(command)
        p.add_argument('output')
        if command == 'check':
            p.add_argument('--names', action='store_true', help='show design names (private) in problem lines')
        if command == 'export':
            p.add_argument('--project', action='append', help='project id from inventory.json (repeatable)')
            p.add_argument('--folder', action='append', metavar='ID:PATH',
                           help='a folder (and its subfolders) in project ID, by names (ID:Parts/Brackets) or by '
                                'folder id (ID:id=FOLDER_ID, see folder_ids in inventory.json); repeatable')
            p.add_argument('--limit', type=int, help='export at most this many designs')
            p.add_argument('--designs-only', action='store_true',
                           help='skip other files (PDFs, drawings, uploads); by default they are saved too')
    sub.add_parser('reset', help='clear stale job files (the button is always available)')
    args = parser.parse_args(argv)
    if args.command == 'reset':
        return reset(addins_dir(system()) / 'FusionBulkExport')
    args.timeout = float(os.environ.get('AUTORUN_TIMEOUT_SECONDS', 86400))
    if args.command == 'status':
        for line in summary(check_output(args.output)):
            print(line)
        return 0
    if args.command == 'check':
        return check(check_output(args.output), args.names)
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
