#!/usr/bin/env python3
"""Unattended Fusion Bulk Export on macOS or Windows.

  autorun.py inventory OUTPUT                       list everything, open nothing
  autorun.py export OUTPUT [--project ID ...] [--folder ID:Folder/Sub ...]
                    [--folder ID:id=FOLDER_ID ...] [--limit N] [--designs-only]
                                                    export (rerun the same command to resume);
                                                    other files (PDFs, uploads) are saved too
                                                    unless --designs-only; drawings and
                                                    electronics are listed as not supported
                                                    (Fusion cannot export them safely)
  autorun.py status OUTPUT                          counts so far (no design names)
  autorun.py check OUTPUT                           check every exported file again (full hashes,
                                                    archives, STEP files); needs no Fusion
  autorun.py stop OUTPUT                            pause an export between designs

The add-in in Fusion is installed or updated from this checkout only when it is missing
or older (by version) than this checkout: an installed add-in is never downgraded, an
installation of the same version is kept, and nothing is replaced while an export runs.
One line says which it used (scripts/install.py replaces it on purpose).

Sign in to Fusion first, and switch the add-in on in Fusion once: Shift+S, find
FusionBulkExport in the list (Search, or tick Add-ins under Type), tick its Run on Startup
box first, then switch Run on (the window closes; that is normal). Until it runs, this
command says so and keeps waiting. If Fusion is closed it is opened. The running add-in always watches for jobs,
and its toolbar button stays available. A job sent while an export
from the button is running is refused with a "busy" message: run the command again later.
Environment: FUSION_APP (app name, .app or .exe path), AUTORUN_TIMEOUT_SECONDS
(default 86400).
"""
import argparse
import glob
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parent.parent


def _installer():
    """scripts/install.py, which owns the one list of run-time files (job files, logs, caches)."""
    spec = importlib.util.spec_from_file_location('fusion_bulk_export_install', str(Path(__file__).with_name('install.py')))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def runtime_files(directory, names):
    """copytree's ignore: the files the add-in creates at run time are never copied over an
    installation (install.is_runtime decides, by exact names, never a broad glob)."""
    is_runtime = _installer().is_runtime
    try:
        relative = Path(directory).resolve().relative_to((REPO / 'FusionBulkExport').resolve())
    except ValueError:
        relative = Path()
    return {name for name in names if is_runtime(relative / name)}
ALIVE_SECONDS = 20
INTERRUPTED = 130  # the exit status after Ctrl-C
SAFE_LOG = re.compile(r'^\S+ (project \d+/\d+ design \d+|finding folder: \d+ folders listed|autorun (start (export|inventory|diagnostics)|finished|busy)|inventory: [\d ,a-z]+|inventory hub \d+/\d+ project \d+/\d+: \d+ files|inventory stopped)$')
MANUAL_HINT_SECONDS = 180
MANUAL_HINT = ('Fusion has not started the add-in. In Fusion press Shift+S and find FusionBulkExport in '
               'the list: type its name in Search, or tick Add-ins under Type on the left. Tick its Run on '
               'Startup box first, then switch Run on. The window closes when you switch it on; that is '
               'normal. This command keeps waiting.')


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


def heartbeat(installed):
    """What the running add-in's autorun.alive says: {"busy": None | "button" | "job"}, or {} when
    it holds no such facts (an add-in from before they were written, or none running)."""
    try:
        value = json.loads((installed / 'autorun.alive').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def exporting(installed, now=None):
    """Whether the running add-in says an export is running now (a fresh heartbeat with busy set)."""
    return alive(installed, now) and bool(heartbeat(installed).get('busy'))


def choose_addin(installed, source=None):
    """(install, line): whether to copy this checkout's add-in over the installed one, and one
    line saying which add-in is used. Installed only when none is there or the installed one is
    older by version; never downgraded; one of the same version is kept (its files may differ:
    a test build, an edited checkout), since which is newer cannot be told."""
    installer = _installer()
    source = Path(source or REPO / 'FusionBulkExport')
    mine = installer.manifest_version(source)
    theirs = installer.manifest_version(installed)
    if theirs is None:
        return True, 'Installing the add-in %s from this checkout (none was installed).' % mine
    mine_key, theirs_key = installer.version_key(mine), installer.version_key(theirs)
    if mine_key is not None and theirs_key is not None and mine_key > theirs_key:
        return True, 'Updating the installed add-in %s to %s from this checkout.' % (theirs, mine)
    if mine_key != theirs_key:
        return False, ('Using the installed add-in %s: it is newer than this checkout (%s) and is never '
                       'downgraded.' % (theirs, mine))
    stamp = installer.read_stamp(installed) or {}
    if stamp.get('sha256') == installer.content_hash(source):
        return False, 'Using the installed add-in %s (the same as this checkout).' % theirs
    return False, ('Using the installed add-in %s: same version as this checkout but other files, so it is '
                   'kept (scripts/install.py replaces it).' % theirs)


def update_addin(installed, source=None):
    """Install this checkout's add-in when choose_addin() says so, unless an export is running
    in Fusion (then nothing is replaced at all). Prints one line; returns whether it copied."""
    if exporting(installed):
        print('An export is running in Fusion: the installed add-in is used as it is (%s).'
              % (_installer().manifest_version(installed) or 'unknown version'), flush=True)
        return False
    copy, line = choose_addin(installed, source)
    print(line, flush=True)
    if not copy:
        return False
    shutil.copytree(str(Path(source or REPO / 'FusionBulkExport')), str(installed), dirs_exist_ok=True,
                    ignore=runtime_files)
    installer = _installer()
    (installed / installer.STAMP).write_text(installer.stamp_text(Path(source or REPO / 'FusionBulkExport')),
                                             encoding='utf-8')
    return True


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


DRAWINGS_REMOVED = ('--drawings does nothing now: drawing export was removed because it can crash Fusion '
                    '(drawings are listed as not supported; export them with File > Export in Fusion).')


def make_output(output):
    """Create the output folder, or stop with one plain line saying why it cannot be."""
    try:
        output.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise SystemExit('The output folder cannot be created: %s (%s). Choose another folder.'
                         % (output, error.strerror or type(error).__name__))


def root_problem(output, name=None):
    """Why output cannot be an export's folder on this system (the add-in's own rule), or None."""
    package = REPO / 'FusionBulkExport'
    if str(package) not in sys.path:
        sys.path.insert(0, str(package))
    from fusion_bulk_export.names import output_root_problem
    return output_root_problem(output, 'win32' if (name or system()) == 'Windows' else 'darwin')


def install(installed, output, mode, projects=None, limit=None, folders=None, designs_only=False):
    make_output(output)
    if not exporting(installed):
        # A running export may be writing to this very folder (its STOP, its result): left alone
        # then. wait() only takes a result written after this command started.
        for stale in ('autorun-result.json', 'STOP'):
            try:
                (output / stale).unlink()
            except FileNotFoundError:
                pass
    update_addin(installed)
    for stale in ('startup-error.txt', 'autorun-output/autorun-result.json'):
        try:
            (installed / stale).unlink()
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
        unchanged = counts.get('files_up_to_date', 0)
        unsupported = counts.get('unsupported', 0)
        return ('Counts: %d exported now, %d already up to date, %d other files saved%s, %d skipped, %d failed%s'
                % (counts.get('exported', 0), counts.get('up_to_date', 0), counts.get('files', 0),
                   ', %d other files already up to date' % unchanged if unchanged else '',
                   counts.get('skipped', 0), counts.get('failed', 0),
                   ("; %d not supported by Fusion's export (electronics, drawings)" % unsupported
                    if unsupported else '')))
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
        # With manifest.journal: the items finished since manifest.json was last written in full.
        entries = package_checks().read_manifest(output).get('entries', [])
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
        unsupported = sum(1 for e in entries if e.get('status') == 'unsupported' or (
            e.get('status') == 'skipped' and e.get('kind') == 'file'
            and str(e.get('error') or '').startswith('Fusion cannot export this file type')))
        if unsupported:
            lines.append("Not supported: %d not supported by Fusion's export (electronics, drawings); "
                         'not failures' % unsupported)
        warned = sum(1 for e in entries if e.get('status') == 'done' and e.get('warnings'))
        if warned:
            lines.append('Warnings: %d item(s) exported with a warning (see the report)' % warned)
        designs = [e for e in entries if (e.get('kind') or 'design') == 'design']
        broken = sum(1 for e in designs if e.get('status') == 'failed' and e.get('fusion_error') is not None)
        if broken:
            lines.append('Links: %d design(s) failed with a linked design missing or not opening (see the report)'
                         % broken)
        unread = sum(1 for e in designs if e.get('status') == 'done' and e.get('references_unreadable'))
        if unread:
            lines.append('Warning: %d design(s): links to other designs could not be read; the design itself '
                         'was exported' % unread)
        slow = sum(1 for e in entries if e.get('slow_seconds'))
        if slow:
            lines.append('Slow: %d item(s) took Fusion over a minute each (the report lists them)' % slow)
        stale = sum(1 for e in designs if e.get('references_out_of_date'))
        if stale:
            lines.append('Note: %d design(s) with out-of-date links (exported as they are, not a failure)' % stale)
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

    def fresh(path):
        try:
            return path.stat().st_mtime >= started_at
        except OSError:
            return False
    while not fresh(result):
        elapsed = clock() - start
        if elapsed >= timeout:
            raise SystemExit('Timed out. Inspect ' + str(output / 'autorun.log'))
        fallback = installed / 'autorun-output' / 'autorun-result.json'
        if fallback.is_file() and fallback.stat().st_mtime >= started_at:
            # The add-in could not use this job's configuration; its report went beside it.
            report = json.loads(fallback.read_text(encoding='utf-8'))
            if report.get('busy'):
                # Answered beside the add-in, never in the output folder (it may be the running export's).
                for error in report.get('errors', []):
                    print('Fusion is busy: ' + error.get('error', ''), flush=True)
                return 1
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
    problem = root_problem(output, name)
    if problem:
        raise SystemExit(problem)
    installed = addins_dir(name) / 'FusionBulkExport'
    watching = alive(installed)
    if getattr(args, 'drawings', False):
        print(DRAWINGS_REMOVED, flush=True)
    install(installed, output, args.command, getattr(args, 'project', None), getattr(args, 'limit', None),
            folder_map(getattr(args, 'folder', None)), getattr(args, 'designs_only', False))
    try:
        if watching:
            return wait(installed, output, args.timeout)
        if fusion_running(name):
            # Open but not watching: the job is in place; starting the add-in by hand picks it up.
            print(MANUAL_HINT, flush=True)
            return wait(installed, output, args.timeout, hint_after=float('inf'))
        # Whether Fusion starts the add-in is Fusion's own Run on Startup setting (kept in Fusion's
        # settings, not in the add-in's manifest), so nothing here can switch it on. If the add-in
        # does not come up, wait() prints MANUAL_HINT once.
        launch_fusion(name)
        return wait(installed, output, args.timeout)
    except KeyboardInterrupt:
        # Ctrl-C: one plain line, never a traceback. A job Fusion never took is withdrawn (that
        # says so); one it took goes on.
        if not withdraw_job(installed):
            print('Stopped waiting; the export goes on in Fusion ("autorun.py stop %s" pauses it).' % output,
                  file=sys.stderr, flush=True)
        return INTERRUPTED
    finally:
        withdraw_job(installed)


def withdraw_job(installed):
    """Remove the job if the add-in never took it (this command was stopped with Ctrl-C, timed
    out, or the add-in failed to start): a later Fusion start must not run it unattended. A job
    the add-in took is gone already (it renames autorun.json as it starts)."""
    try:
        (installed / 'autorun.json').unlink()
    except FileNotFoundError:
        return False
    except OSError:
        return False
    print('The job was not started by Fusion and has been withdrawn.', file=sys.stderr, flush=True)
    return True


def package_checks():
    """The add-in's checks module (stdlib only), from this checkout."""
    package = REPO / 'FusionBulkExport'
    if str(package) not in sys.path:
        sys.path.insert(0, str(package))
    from fusion_bulk_export import checks
    return checks


def reset(installed):
    """Kept for old instructions: clears a job file that was never picked up. The toolbar button
    is always there. A running job finishes first."""
    removed = []
    for name in ('autorun.json',):
        try:
            (installed / name).unlink()
            removed.append(name)
        except FileNotFoundError:
            pass
    print('Nothing to reset: the Fusion Bulk Export button is always available%s.'
          % (' (removed stale ' + ', '.join(removed) + ')' if removed else ''))
    return 0


def check(output, names=False):
    """Check this folder: every exported file again, without Fusion. Counts only unless names."""
    checks = package_checks()
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
          '%d STEP file(s) missing bodies, %d not exported yet (failed).'
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
            p.add_argument('--limit', type=int, metavar='N',
                           help='stop after exporting N designs in this run (designs already up to date and '
                                'other files do not count). Other files are still saved in every folder '
                                'visited up to then, including the rest of the folder where the Nth design '
                                'was exported; later designs and folders are left for the next run')
            p.add_argument('--designs-only', action='store_true',
                           help='skip other files (PDFs, uploads); by default they are saved too')
            # Kept for one release so older instructions still run; it only prints DRAWINGS_REMOVED.
            p.add_argument('--drawings', action='store_true', help=argparse.SUPPRESS)
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
    try:
        return start(args)
    except KeyboardInterrupt:  # before the job was sent (start handles it once it is)
        print('Stopped.', file=sys.stderr, flush=True)
        return INTERRUPTED


if __name__ == '__main__':
    sys.exit(main())
