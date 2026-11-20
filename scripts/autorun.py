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
(default 86400), FUSION_BULK_EXPORT_MEMORY_GUARD=off (switches the memory guard off for the job).

Exit status: 0 done; 1 the job failed or was refused; 75 the export stopped itself because
Fusion was using a lot of memory (restart Fusion and run the same command again: it carries on);
76 --restart-fusion opened Fusion again but the add-in did not start (switch on its Run on
Startup); 77 Fusion closed or crashed before the export finished (open it and run again);
130 interrupted (Ctrl-C).

export --restart-fusion (opt-in) does that by itself: after a memory stop it quits Fusion
politely (only when no saved design or unsaved document is open in it), opens it again, waits
for the add-in and sends the same job; after a crash it opens Fusion again. At most
--max-restarts times (default 20), never more than once per 50 designs exported: a memory stop
within 50 designs of a start ends the command with status 75 and one line saying so.
--restart-fusion=force also ends a Fusion that does not quit within 60 s.
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


def words():
    """The add-in's words module (counts in words: "1 design", "8 designs"), from this checkout."""
    package = REPO / 'FusionBulkExport'
    if str(package) not in sys.path:
        sys.path.insert(0, str(package))
    from fusion_bulk_export import words as module
    return module


def plural(count, word, many=None):
    return words().plural(count, word, many)


def form(count, word, many=None):
    return words().form(count, word, many)


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
# Exit status of an export that stopped itself because Fusion was using a lot of memory: restart
# Fusion and run the same command again (it carries on), or let --restart-fusion do that.
EXIT_MEMORY = 75
MEMORY_LINE = ('Stopped: Fusion is using a lot of memory. Close Fusion, open it again and run the same command: '
               'it carries on where it stopped (--restart-fusion does this by itself).')
# Exit status when Fusion closed (or crashed) before the export finished: open it and run again.
EXIT_FUSION_GONE = 77
GONE_LINE = ('Fusion closed before the export finished (it may have crashed). Open Fusion and run the same '
             'command again: it carries on where it stopped.')
# Exit status when --restart-fusion opened Fusion again but the add-in did not start in it.
EXIT_NO_ADDIN = 76
SAFE_LOG = re.compile(r'^\S+ (project \d+/\d+ design \d+|finding folder: \d+ folders listed|autorun (start (export|inventory|diagnostics)|finished|busy)|inventory: [\d ,a-z]+|inventory hub \d+/\d+ project \d+/\d+: \d+ files?|inventory stopped)$')
# A job the add-in took (autorun.json gone) while it is idle (heartbeat not busy) and no result
# anywhere for this long: it could not answer (wait() stops with LOST_JOB_LINE, exit status 1).
LOST_JOB_SECONDS = 60
LOST_JOB_LINE = ('Fusion took the job but wrote no result, and it is not exporting. It may not reach the output '
                 'folder (a network drive must be connected for Fusion too): choose another folder, or run the '
                 'same command again.')
MANUAL_HINT_SECONDS = 180
MANUAL_STEPS = ('Fusion has not started the add-in. In Fusion press Shift+S and find FusionBulkExport in '
                'the list: type its name in Search, or tick Add-ins under Type on the left. Tick its Run on '
                'Startup box first, then switch Run on. The window closes when you switch it on; that is '
                'normal.')
MANUAL_HINT = MANUAL_STEPS + ' This command keeps waiting.'


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
    a test build, an edited checkout), since which is newer cannot be told.

    The installed copy is judged by its files, not by its install-stamp.json, which may be
    missing (copied by hand) or old: the same files are the same add-in. The same files with
    another version in the manifest (an install that copied everything but the manifest and the
    stamp) are this checkout with a stale label: the label is corrected, and the line says so."""
    installer = _installer()
    source = Path(source or REPO / 'FusionBulkExport')
    mine = installer.manifest_version(source)
    theirs = installer.manifest_version(installed)
    if theirs is None:
        return True, 'Installing the add-in %s from this checkout (none was installed).' % mine
    if installer.content_hash(installed) == installer.content_hash(source):
        return False, 'Using the installed add-in %s (the same as this checkout).' % theirs
    label = (installer.MANIFEST,)
    if installer.content_hash(installed, leave_out=label) == installer.content_hash(source, leave_out=label):
        return True, ('The installed add-in has the files of this checkout (%s) but an out-of-date version label '
                      '(%s); correcting the label.' % (mine, theirs))
    mine_key, theirs_key = installer.version_key(mine), installer.version_key(theirs)
    if mine_key is not None and theirs_key is not None and mine_key > theirs_key:
        return True, 'Updating the installed add-in %s to %s from this checkout.' % (theirs, mine)
    if mine_key != theirs_key:
        return False, ('Using the installed add-in %s: it is newer than this checkout (%s) and is never '
                       'downgraded.' % (theirs, mine))
    return False, ('Using the installed add-in %s: installed copy differs from this checkout; kept the installed '
                   'one; run scripts/install.py to replace it.' % theirs)


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
    """The output folder as an absolute path, never resolved: a mapped network drive (X:) keeps
    its letter (resolve() gives its \\\\server\\share path on Windows). Its length is not limited:
    the add-in writes long paths itself (and says so if a folder cannot be used)."""
    return Path(os.path.normpath(os.path.abspath(os.path.expanduser(str(output)))))


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
    """Create the output folder, or stop with one plain line saying why it cannot be. True when
    this call created it (so a rejected job can take it away again: drop_new_output)."""
    existed = output.is_dir()
    try:
        output.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise SystemExit('The output folder cannot be created: %s (%s). Choose another folder.'
                         % (output, error.strerror or type(error).__name__))
    return not existed


def drop_new_output(output):
    """Remove the output folder this command just created, if it is still empty (a job the
    add-in rejected leaves nothing behind). A folder with anything in it is never touched."""
    try:
        output.rmdir()  # only an empty folder can go
    except OSError:
        pass


def root_problem(output, name=None):
    """Why output cannot be an export's folder on this system (the add-in's own rule), or None."""
    package = REPO / 'FusionBulkExport'
    if str(package) not in sys.path:
        sys.path.insert(0, str(package))
    from fusion_bulk_export.names import output_root_problem
    return output_root_problem(output, 'win32' if (name or system()) == 'Windows' else 'darwin')


def install(installed, output, mode, projects=None, limit=None, folders=None, designs_only=False):
    """Put the job in place (after updating the add-in when needed); True when the output folder
    was created for it."""
    created = make_output(output)
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
    if not memory_guard_wanted():
        config['memory_guard'] = False
    experiment = linked_copies_experiment()
    if experiment:
        config['experiment_linked_copies'] = experiment
    (installed / 'autorun.json').write_text(json.dumps(config, indent=2) + '\n', encoding='utf-8')
    return created


MEMORY_GUARD_ENV = 'FUSION_BULK_EXPORT_MEMORY_GUARD'  # the add-in's core.MEMORY_GUARD_ENV


def memory_guard_wanted(environ=None):
    """False when this command's environment switches the memory guard off (off, 0, false, no):
    the job then tells the add-in, whose own environment (Fusion's) is out of reach here."""
    value = (os.environ if environ is None else environ).get(MEMORY_GUARD_ENV)
    return not (value is not None and value.strip().lower() in ('off', '0', 'false', 'no'))


# Testing only, not for users: the add-in's core.LINKED_COPIES_ENV. Passed on in the job like
# MEMORY_GUARD_ENV, so a test run started here can switch the experimental copies with linked
# parts on ("1", "innermost" or "compute"); unset or anything else leaves them off.
LINKED_COPIES_ENV = 'FUSION_BULK_EXPORT_EXPERIMENT_LINKED_COPIES'
LINKED_COPIES_VALUES = ('1', 'both', 'innermost', 'compute')  # the add-in's core.LINK_RECIPES


def linked_copies_experiment(environ=None):
    """The LINKED_COPIES_ENV value to pass on in the job (lower case), or None (not set or not
    a value the add-in knows: off)."""
    value = (os.environ if environ is None else environ).get(LINKED_COPIES_ENV)
    value = value.strip().lower() if isinstance(value, str) else None
    return value if value in LINKED_COPIES_VALUES else None


ACCOUNT_CHANGED_LINE = ("Stopped by itself (account_changed): a design's version or date in the Fusion account "
                        "changed while it was exported with its linked parts. Do not run again; check the "
                        "design's version history in Fusion and report it (export.log in the output folder).")
LOW_MEMORY_NOTE = 'This computer was low on memory during the export; closing other programs may speed it up.'


def count_line(counts):
    """The run's counts in words; results of versions before 0.3 had done/failed/skipped."""
    if 'exported' in counts:
        unchanged = counts.get('files_up_to_date', 0)
        unsupported = counts.get('unsupported', 0)
        return ('Counts: %d exported now, %d already up to date, %s saved%s, %d skipped, %d failed%s'
                % (counts.get('exported', 0), counts.get('up_to_date', 0), plural(counts.get('files', 0), 'other file'),
                   ', %s already up to date' % plural(unchanged, 'other file') if unchanged else '',
                   counts.get('skipped', 0), counts.get('failed', 0),
                   ("; %s can't be exported by an add-in (drawings, electronics)" % plural(unsupported, 'file')
                    if unsupported else '')))
    return 'Counts: ' + json.dumps(counts, sort_keys=True)


def memory_line(facts):
    """'Fusion memory: 1.4 GB at the end (at most 1.6 GB, 0.8 GB at the start)' from the result's
    memory facts, or None when the add-in did not measure it."""
    facts = facts if isinstance(facts, dict) else {}
    size = lambda key: None if not isinstance(facts.get(key), int) else '%.1f GB' % (facts[key] / float(1 << 30))
    last, peak, start = size('last_rss'), size('peak_rss'), size('start_rss')
    if last is None:
        return None
    more = [text for text in ('at most ' + peak if peak else None, start + ' at the start' if start else None) if text]
    return 'Fusion memory: %s at the end%s' % (last, ' (%s)' % ', '.join(more) if more else '')


def folder_memory_line(manifest):
    """The Fusion memory line over every run into this folder (each launch is a run: a long
    export resumed after stops is several), from the manifest's runs: the last run's end, the
    highest of all, the first run's start. None when no run measured it."""
    measured = [run['memory'] for run in (manifest or {}).get('runs') or []
                if isinstance(run, dict) and isinstance(run.get('memory'), dict)
                and isinstance(run['memory'].get('last_rss'), int)]
    if len(measured) < 2:
        return memory_line(measured[0]) if measured else None
    size = lambda value: '%.1f GB' % (value / float(1 << 30))
    peaks = [facts['peak_rss'] for facts in measured if isinstance(facts.get('peak_rss'), int)]
    first = measured[0].get('start_rss')
    more = []
    if peaks:
        more.append('at most %s over the %d runs into this folder' % (size(max(peaks)), len(measured)))
    if isinstance(first, int):
        more.append('%s at the start of the first' % size(first))
    return 'Fusion memory: %s at the end%s' % (size(measured[-1]['last_rss']), ' (%s)' % ', '.join(more) if more else '')


def summary(output):
    """Counts only: manifests and logs hold design names, which stay private."""
    lines = []
    result = output / 'autorun-result.json'
    if result.is_file():
        report = json.loads(result.read_text(encoding='utf-8'))
        lines.append('Result: mode %s, finished %s, Fusion %s' % (
            report.get('mode'), report.get('finished'), report.get('fusion_version')))
        lines.append(count_line(report.get('counts', {})))
        if report.get('stopped_code') == 'memory':
            lines.append('Stopped by itself (memory): Fusion is using a lot of memory. Restart Fusion, then run '
                         'export again to carry on.')
        elif report.get('stopped_code') == 'account_changed':
            lines.append(ACCOUNT_CHANGED_LINE)
        elif report.get('stopped_reason'):
            lines.append('Stopped by itself (%s); run export again to carry on.' % report.get('stopped_code'))
        if report.get('low_memory') and report.get('stopped_code') != 'memory':
            lines.append('Note: ' + LOW_MEMORY_NOTE)
        if report.get('low_space'):
            lines.append('Low disk space: %d MB free' % ((report.get('free_bytes') or 0) >> 20))
        memory = None
        if (output / 'manifest.json').is_file():
            try:
                memory = folder_memory_line(package_checks().read_manifest(output))
            except (OSError, ValueError):
                memory = None
        memory = memory or memory_line(report.get('memory'))
        if memory:
            lines.append(memory)
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
            lines.append('%s: %s %s%s' % (label, plural(len(chosen), 'entry'), json.dumps(statuses, sort_keys=True),
                                                  ', %d bytes' % size if kind is None else ''))
        unsupported = sum(1 for e in entries if e.get('status') == 'unsupported' or (
            e.get('status') == 'skipped' and e.get('kind') == 'file'
            and str(e.get('error') or '').startswith('Fusion cannot export this file type')))
        if unsupported:
            lines.append("Not supported: %s can't be exported by an add-in (drawings, electronics); %s not %s"
                         % (plural(unsupported, 'file'), form(unsupported, 'it is', 'they are'),
                            form(unsupported, 'a failure', 'failures')))
        warned = sum(1 for e in entries if e.get('status') == 'done' and e.get('warnings'))
        if warned:
            lines.append('Warnings: %s exported with a warning (see the report)' % plural(warned, 'item'))
        designs = [e for e in entries if (e.get('kind') or 'design') == 'design']
        broken = sum(1 for e in designs if e.get('status') == 'failed' and e.get('fusion_error') is not None)
        if broken:
            lines.append('Links: %s failed with a linked design missing or not opening (see the report)'
                         % plural(broken, 'design'))
        unread = sum(1 for e in designs if e.get('status') == 'done' and e.get('references_unreadable'))
        if unread:
            lines.append('Warning: %s whose links to other designs could not be read; %s exported all the same'
                         % (plural(unread, 'design'), form(unread, 'it was', 'they were')))
        slow = sum(1 for e in entries if e.get('slow_seconds'))
        if slow:
            lines.append('Slow: %s took Fusion over a minute%s (the report lists %s)'
                         % (plural(slow, 'item'), '' if slow == 1 else ' each', form(slow, 'it', 'them')))
        stale = sum(1 for e in designs if e.get('references_out_of_date'))
        if stale:
            lines.append('Note: %s with out-of-date links (exported as %s, not a failure)'
                         % (plural(stale, 'design'), form(stale, 'it is', 'they are')))
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


WAITING_LINE_SECONDS = 5  # an answer within this long (a rejected job) comes without "Waiting for"


def wait(installed, output, timeout, hint_after=MANUAL_HINT_SECONDS, sleep=time.sleep, clock=time.monotonic,
         running=None):
    """Wait for this job's autorun-result.json and print its summary; the exit status. Once the
    add-in's heartbeat has been seen, a heartbeat gone stale while Fusion's process is gone too
    means Fusion closed or crashed mid-run: EXIT_FUSION_GONE. (A stale heartbeat alone is not:
    Fusion can be busy for minutes in one design.)"""
    result = output / 'autorun-result.json'
    waiting = False  # the "Waiting for" line, printed only once an answer is not there at once
    start = clock()
    started_at = time.time() - 5
    hinted = False
    seen = False  # the add-in's heartbeat was fresh at some point while waiting
    lost = None   # since when the job is taken, the add-in idle, and no result written
    running = running or fusion_running

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
            if isinstance(report.get('counts'), dict):
                # It ran, but its output folder could not take the result.
                print(count_line(report['counts']), flush=True)
            errors = [str(error.get('error', '')) for error in report.get('errors', [])] or ['no reason given']
            for error in errors:
                print('The add-in rejected the job: ' + ' '.join(error.split()), flush=True)
            return 1
        if not waiting and elapsed >= WAITING_LINE_SECONDS:
            print('Waiting for ' + str(result) + ' (progress: autorun.log)', flush=True)
            waiting = True
        crash = installed / 'startup-error.txt'
        if crash.is_file():
            print('The add-in failed to start:\n' + crash.read_text(encoding='utf-8'), flush=True)
            return 1
        if not hinted and elapsed >= hint_after and not (installed / 'autorun.alive').exists():
            print(MANUAL_HINT, flush=True)
            hinted = True
        if alive(installed):
            seen = True
            beat = heartbeat(installed)
            # The add-in took the job (it renames autorun.json as it starts) and is not running it,
            # yet no result appeared anywhere: it could not answer. Not waiting a day for nothing.
            # (Only with an add-in whose heartbeat says whether it is busy.)
            if 'busy' in beat and not beat.get('busy') and not (installed / 'autorun.json').exists():
                lost = clock() if lost is None else lost
                if clock() - lost >= LOST_JOB_SECONDS:
                    print(LOST_JOB_LINE, flush=True)
                    return 1
            else:
                lost = None
        elif seen and not running() and not fresh(result):
            print(GONE_LINE, flush=True)
            return EXIT_FUSION_GONE
        sleep(5)
    report = json.loads(result.read_text(encoding='utf-8'))
    if report.get('busy'):
        for error in report.get('errors', []):
            print('Fusion is busy: ' + error.get('error', ''), flush=True)
        return 1
    for line in summary(output):
        print(line)
    if any(e.get('stage') == 'autorun' for e in report.get('errors', [])):
        return 1
    if report.get('stopped_code') == 'memory':
        print(MEMORY_LINE, flush=True)
        return EXIT_MEMORY
    if report.get('stopped_code') == 'account_changed':
        return 1
    return 0


def start(args):
    name = system()
    output = check_output(args.output)
    problem = root_problem(output, name)
    if problem:
        raise SystemExit(problem)
    installed = addins_dir(name) / 'FusionBulkExport'
    if getattr(args, 'drawings', False):
        print(DRAWINGS_REMOVED, flush=True)
    mode = getattr(args, 'restart_fusion', None) if args.command == 'export' else None
    if not mode:
        return run_job(args, name, output, installed)
    return with_restarts(lambda: run_job(args, name, output, installed), FusionProcess(name, installed),
                         output, force=mode == 'force', limit=getattr(args, 'max_restarts', MAX_RESTARTS))


def run_job(args, name, output, installed):
    """Send the job to the add-in (opening Fusion if it is closed) and wait for its result. An
    output folder this command created and the job left empty (a rejected job) is removed again."""
    watching = alive(installed)
    created = install(installed, output, args.command, getattr(args, 'project', None), getattr(args, 'limit', None),
                      folder_map(getattr(args, 'folder', None)), getattr(args, 'designs_only', False))
    try:
        status = send(args, name, output, installed, watching)
    except KeyboardInterrupt:
        # Ctrl-C: one plain line, never a traceback. A job Fusion never took is withdrawn (that
        # says so); one it took goes on.
        if not withdraw_job(installed):
            print('Stopped waiting; the export goes on in Fusion ("autorun.py stop %s" pauses it).' % output,
                  file=sys.stderr, flush=True)
        return INTERRUPTED
    finally:
        withdraw_job(installed)
    if status == 1 and created:
        drop_new_output(output)
    return status


def send(args, name, output, installed, watching):
    """Wait for the job's result, opening Fusion first if it is closed; the exit status."""
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


MAX_RESTARTS = 20           # --restart-fusion restarts Fusion at most this many times in one command...
DESIGNS_PER_RESTART = 50    # ...and only after at least this many designs were exported since the last start
# A memory stop within DESIGNS_PER_RESTART designs of a start: restarting would not help (and could
# loop), so the command stops there with EXIT_MEMORY and this line.
EARLY_MEMORY_LINE = ("Not restarting Fusion: it stopped for memory after only %d %s since Fusion was started: "
                     "Fusion's memory is high for another reason, so a restart would not help. Close other "
                     "programs (or restart the computer), then run the same command again.")
QUIT_SECONDS = 60           # how long Fusion gets to quit when asked politely
KILL_SECONDS = 30           # how long a forced kill gets to take effect


class FusionProcess:
    """What --restart-fusion does to Fusion, one method per action (the tests use a fake)."""
    def __init__(self, name, installed, sleep=time.sleep, clock=time.monotonic):
        self.name, self.installed, self.sleep, self.clock = name, installed, sleep, clock

    def running(self):
        return fusion_running(self.name)

    def quit(self):
        """Ask Fusion to quit, as the user would (it may refuse or ask about unsaved work)."""
        if self.name == 'Windows':
            command = ['taskkill', '/IM', 'Fusion360.exe']  # no /F: a close request, not a kill
        else:
            command = ['osascript', '-e', 'tell application "Autodesk Fusion" to quit']
        subprocess.run(command, capture_output=True)

    def kill(self):
        if self.name == 'Windows':
            command = ['taskkill', '/F', '/IM', 'Fusion360.exe']
        else:
            command = ['pkill', '-KILL', '-x', 'Autodesk Fusion']
        subprocess.run(command, capture_output=True)

    def launch(self):
        launch_fusion(self.name)

    def started_since(self, moment):
        """Whether the add-in's heartbeat was written after moment (time.time()) and is fresh."""
        try:
            return (self.installed / 'autorun.alive').stat().st_mtime > moment and alive(self.installed)
        except OSError:
            return False


def done_designs(output):
    """How many designs the folder's manifest (with its journal) holds as exported."""
    try:
        entries = package_checks().read_manifest(output).get('entries', [])
    except (OSError, ValueError):
        return 0
    return sum(1 for e in entries if (e.get('kind') or 'design') == 'design' and e.get('status') == 'done')


def read_result(output):
    try:
        value = json.loads((output / 'autorun-result.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def restart_decision(status, result, restarts, limit, exported):
    """(restart, line): whether --restart-fusion may restart Fusion after a run that ended with
    status, and one plain line saying what happens. result: the run's autorun-result.json (empty
    when Fusion closed before writing it); restarts: done so far in this command; exported:
    designs exported since Fusion was last started by this command (or since it began)."""
    if status not in (EXIT_MEMORY, EXIT_FUSION_GONE):
        return False, None
    if restarts >= limit:
        return False, ('Not restarting Fusion again: it was restarted %s in this command already.'
                       % plural(restarts, 'time'))
    if exported < DESIGNS_PER_RESTART:
        if status == EXIT_MEMORY:
            return False, EARLY_MEMORY_LINE % (exported, form(exported, 'design'))
        return False, ('Not restarting Fusion: only %s %s exported since it was started, so restarting '
                       'would not help.' % (plural(exported, 'design'), form(exported, 'was')))
    if status == EXIT_FUSION_GONE:
        return True, 'Fusion closed before the export finished; opening it again to carry on.'
    work = result.get('open_documents_with_work')
    if not isinstance(work, int):
        return False, 'Not restarting Fusion: the add-in did not say which documents are open in it.'
    if work:
        return False, ('Not restarting Fusion: %s in it %s a saved design or unsaved changes. '
                       'Close %s, then run the same command again.'
                       % (plural(work, 'open document'), form(work, 'holds'), form(work, 'it')))
    return True, 'Restarting Fusion to give its memory back, then carrying on.'


def restart_fusion(fusion, status, force, hint_seconds=MANUAL_HINT_SECONDS):
    """Quit Fusion politely (after a memory stop; after a crash it is gone already), with a
    forced kill only when force is set, open it again and wait for the add-in. None when the
    add-in is running again; else the exit status to stop with."""
    if fusion.running():
        if status == EXIT_FUSION_GONE:
            pass  # opened again meanwhile (by the user): used as it is
        else:
            fusion.quit()
            if not gone(fusion, QUIT_SECONDS):
                if not force:
                    print('Fusion did not quit within %d s (it may be asking something). Close it, then run the '
                          'same command again.' % QUIT_SECONDS, flush=True)
                    return status
                print('Fusion did not quit within %d s; ending it (--restart-fusion=force).' % QUIT_SECONDS, flush=True)
                fusion.kill()
                if not gone(fusion, KILL_SECONDS):
                    print('Fusion could not be ended. Close it, then run the same command again.', flush=True)
                    return status
    moment = time.time()
    if not fusion.running():
        fusion.launch()
    began = fusion.clock()
    while not fusion.started_since(moment):
        if fusion.clock() - began >= hint_seconds:
            print(MANUAL_STEPS + ' Then run the same command again.', flush=True)
            return EXIT_NO_ADDIN
        fusion.sleep(5)
    return None


def gone(fusion, seconds):
    """Wait up to seconds for Fusion's process to end; whether it did."""
    began = fusion.clock()
    while fusion.running():
        if fusion.clock() - began >= seconds:
            return False
        fusion.sleep(2)
    return True


def with_restarts(run, fusion, output, force=False, limit=MAX_RESTARTS):
    """run() the job; while it ends with a memory stop or Fusion gone and restart_decision()
    allows, restart Fusion and run the same job again. The last run's exit status."""
    since = done_designs(output)
    restarts = 0
    status = run()
    while True:
        done = done_designs(output)
        allowed, line = restart_decision(status, read_result(output) if status == EXIT_MEMORY else {},
                                         restarts, limit, done - since)
        if line:
            print(line, flush=True)
        if not allowed:
            return status
        stopped = restart_fusion(fusion, status, force)
        if stopped is not None:
            return stopped
        restarts += 1
        since = done
        print('Fusion restarted (%d of at most %d); carrying on.' % (restarts, limit), flush=True)
        status = run()


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
    print('Checked %s: %d with a problem, %s, %s, %s missing bodies, %d not exported yet (failed).'
          % (plural(totals['done'], 'finished item'), totals['failed_checks'] - totals['failed'],
             plural(totals['missing'], 'missing file'), plural(totals['changed'], 'changed file'),
             plural(totals['solids_differ'], 'STEP file'), totals['failed']))
    print(json.dumps(totals))
    return 0 if not totals['failed_checks'] else 1


EXPORT_EPILOG = """Exit status: 0 done; 1 the job failed or was refused; 75 the export stopped itself because
Fusion was using a lot of memory (restart Fusion and run the same command again: it carries on;
--restart-fusion does that by itself, except within 50 designs of a start, when a restart would
not help); 76 --restart-fusion opened Fusion again but the add-in did not start (switch on its
Run on Startup); 77 Fusion closed or crashed before the export finished (open it and run again);
130 interrupted (Ctrl-C)."""


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('inventory', 'export', 'diagnostics', 'status', 'stop', 'check'):
        p = sub.add_parser(command, **(dict(epilog=EXPORT_EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
                                       if command == 'export' else {}))
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
            p.add_argument('--restart-fusion', nargs='?', const='polite', choices=('polite', 'force'),
                           metavar='force',
                           help='closes and reopens Fusion by itself after a memory stop or crash, then carries on; '
                                '=force may end Fusion, losing anything unsaved.')
            p.add_argument('--max-restarts', type=int, default=MAX_RESTARTS, metavar='N',
                           help='with --restart-fusion: restart Fusion at most N times (default %d), and never '
                                'more than once per %d designs exported' % (MAX_RESTARTS, DESIGNS_PER_RESTART))
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
