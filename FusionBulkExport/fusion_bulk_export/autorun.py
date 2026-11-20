"""Unattended export started from autorun.json: no dialogs, progress in files.

Configuration (all paths absolute):
  {"output": "/path/out",            required; same folder resumes
   "mode": "export" | "inventory" | "diagnostics",   default "export"
   "projects": ["<project id>", ...],  optional; whole projects (default: everything)
   "folders": {"<project id>": ["Folder/Subfolder", ...]},  optional; chosen folders
   "limit": 25,                      optional; export at most this many designs
   "hub_timeout_seconds": 600}       optional; wait for hubs after sign-in

"diagnostics" writes diagnostics.json (Fusion version, the export-related API
members and text commands this build offers) for bug reports; it reads no designs.
"inventory" writes inventory.json (every hub, project and design with ids and
versions) without opening anything. An export also writes index.html and index.csv (the
human-readable report) into the output folder; counts in autorun-result.json are exported,
up_to_date, skipped and failed. Creating a file named STOP in the output
folder ends an export between designs; run again to resume.

A job that arrives while an export from the toolbar button is running is not started: its
autorun-result.json says busy (true) with the reason, and the command can simply be run again.
"""
import json
from pathlib import Path
import re
import time
import traceback

import adsk
import adsk.core

from .core import Exporter, Listing, Selection, atomic_write, collection, now, select_projects, walk
from .fusion import FusionAdapter
from .report import write as write_report


class Log:
    def __init__(self, output):
        self.output = output
        self.result = dict(schema=1, started=now(), finished=None, mode=None, fusion_version=None,
                           errors=[], timings={})

    def write(self, message):
        with (self.output / 'autorun.log').open('a', encoding='utf-8') as stream:
            stream.write(now() + ' ' + message + '\n')

    def error(self, stage, error):
        detail = dict(stage=stage, error=str(error), traceback=traceback.format_exc())
        self.result['errors'].append(detail)
        self.write(stage + ': ' + detail['traceback'])


def read_config(config_path):
    text = config_path.read_text(encoding='utf-8')
    # One-shot: later Fusion launches start normally instead of exporting again.
    config_path.replace(config_path.with_name(config_path.name + '.last'))
    config = json.loads(text)
    output = Path(config['output']).expanduser()
    if not output.is_absolute():
        raise ValueError('output must be an absolute path')
    try:
        return (output,) + check_settings(config)
    except Exception as error:
        error.output = output  # report the problem in the folder the user asked for
        raise


def check_settings(config):
    mode = config.get('mode', 'export')
    if mode not in ('export', 'inventory', 'diagnostics'):
        raise ValueError('mode must be "export", "inventory" or "diagnostics"')
    projects = config.get('projects')
    if projects is not None and (not isinstance(projects, list) or not projects):
        raise ValueError('projects must be a nonempty list of project ids')
    folders = config.get('folders')
    if folders is not None and (not isinstance(folders, dict) or not folders
                                or not all(isinstance(v, list) and v for v in folders.values())):
        raise ValueError('folders must map project ids to nonempty lists of folder paths')
    selection = Selection.from_config(projects, folders)
    limit = config.get('limit')
    if limit is not None and (not isinstance(limit, int) or limit < 1):
        raise ValueError('limit must be a positive integer')
    timeout = float(config.get('hub_timeout_seconds', 600))
    if not 0 < timeout <= 86400:
        raise ValueError('hub_timeout_seconds must be between 0 and 86400')
    return mode, selection, limit, timeout


def wait_for_hubs(app, timeout, log):
    deadline = time.monotonic() + timeout
    while not app.data.dataHubs.count:
        if time.monotonic() >= deadline:
            raise TimeoutError('No hubs appeared; is Fusion signed in?')
        adsk.doEvents()
        time.sleep(0.5)


DIAGNOSTIC_CLASSES = [('core', 'DataFile'), ('core', 'Document'), ('core', 'DataFolder'),
                      ('fusion', 'ExportManager'), ('fusion', 'FusionArchiveExportOptions'),
                      ('fusion', 'FusionDocument'), ('core', 'Application')]
TEXT_COMMAND = re.compile(r'archive|f3z|f3d|export|download', re.IGNORECASE)


def diagnostics(app):
    """API surface relevant to exporting, from this Fusion build. Contains no account data."""
    import adsk.fusion
    modules = dict(core=adsk.core, fusion=adsk.fusion)
    classes = {}
    for module, name in DIAGNOSTIC_CLASSES:
        cls = getattr(modules[module], name, None)
        if cls is None:
            classes[name] = None
            continue
        members = {}
        for member in sorted(m for m in dir(cls) if not m.startswith('_')):
            doc = getattr(getattr(cls, member, None), '__doc__', None) or ''
            members[member] = doc.strip()[:600]
        classes[name] = members
    try:
        listing = app.executeTextCommand('TextCommands.List /hidden')
        commands = [line.strip() for line in listing.splitlines() if TEXT_COMMAND.search(line)]
    except Exception as error:
        commands = ['unavailable: ' + str(error)]
    return dict(fusion_version=app.version, classes=classes, text_commands=commands)


def inventory(data, log, stop=lambda: False, output=None):
    """List every hub, project and file without opening anything.

    Logs one counts-only line per project, saves inventory.json after each project so a long
    listing can be used (or resumed by eye) before it ends, and stops between projects when
    stop() is true.
    """
    hubs, errors, stopped = {}, [], False

    def unreadable(where, error):
        errors.append(dict(path=where, error=str(error)))

    def summary():
        projects = [p for h in hubs.values() for p in h['projects'].values()]
        files = [f for p in projects for f in p['files']]
        return dict(hubs=hubs, unreadable=errors, stopped=stopped,
                    counts=dict(hubs=len(hubs), projects=len(projects), files=len(files),
                                designs=sum(1 for f in files if f['extension'].lower() == 'f3d'),
                                unreadable=len(errors)))

    hub_list = list(collection(data.dataHubs))
    for hub_number, hub in enumerate(hub_list, 1):
        h = hubs.setdefault(str(hub.id), dict(name=hub.name, projects={}))
        try:
            projects = list(collection(hub.dataProjects))
        except Exception as error:
            unreadable([hub.name], error)
            continue
        for project_number, project in enumerate(projects, 1):
            if stop():
                stopped = True
                break
            p = h['projects'][str(project.id)] = dict(name=project.name, files=[])
            view = _one_project(hub, project)
            for _, _, path, file in walk(view, unreadable):
                adsk.doEvents()
                p['files'].append(dict(id=str(file.id), name=file.name, path=path,
                                       extension=file.fileExtension, version=int(file.versionNumber)))
            log.write('inventory hub %d/%d project %d/%d: %d files'
                      % (hub_number, len(hub_list), project_number, len(projects), len(p['files'])))
            if output is not None:
                atomic_write(output / 'inventory.json', summary())
        if stopped:
            log.write('inventory stopped')
            break
    listing = summary()
    counts = listing['counts']
    log.write('inventory: %d hubs, %d projects, %d files, %d designs, %d unreadable'
              % (counts['hubs'], counts['projects'], counts['files'], counts['designs'], counts['unreadable']))
    return listing


def _one_project(hub, project):
    """Data view holding just this project, without listing the hub's projects again."""
    hub_view = type('HubView', (), {})()
    hub_view.id, hub_view.name, hub_view.dataProjects = hub.id, hub.name, Listing([project])
    view = type('DataView', (), {})()
    view.dataHubs = Listing([hub_view])
    return view


BUSY = ('An export started from the toolbar button is running in Fusion. '
        'Run this command again when it has finished.')


def reject_busy(app, config_path, reason=BUSY):
    """Answer a job that cannot start now: consume autorun.json and write a "busy" result where
    the launcher waits for it (the job's output folder, else autorun-output beside the add-in)."""
    config_path = Path(config_path)
    output = config_path.parent / 'autorun-output'
    try:
        text = config_path.read_text(encoding='utf-8')
        config_path.replace(config_path.with_name(config_path.name + '.last'))
        wanted = Path(json.loads(text)['output']).expanduser()
        if wanted.is_absolute():
            output = wanted
    except Exception:
        pass  # an unreadable job is answered beside the add-in
    output.mkdir(parents=True, exist_ok=True)
    log = Log(output)
    log.result.update(busy=True, finished=now(), fusion_version=getattr(app, 'version', None),
                      errors=[dict(stage='busy', error=reason, traceback='')])
    log.write('autorun busy')
    atomic_write(output / 'autorun-result.json', log.result)
    return log.result


def run_autorun(app, config_path):
    config_path = Path(config_path)
    log = None
    started = time.monotonic()
    try:
        try:
            output, mode, selection, limit, timeout = read_config(config_path)
        except Exception as error:
            output = getattr(error, 'output', None) or config_path.parent / 'autorun-output'
            raise
        finally:
            output.mkdir(parents=True, exist_ok=True)
            log = Log(output)
        log.result.update(mode=mode, fusion_version=app.version, selection=selection.describe(), limit=limit)
        log.write('autorun start ' + mode)
        if mode == 'diagnostics':
            report = diagnostics(app)
            atomic_write(output / 'diagnostics.json', report)
            log.result['counts'] = dict(classes=sum(1 for v in report['classes'].values() if v is not None),
                                        text_commands=len(report['text_commands']))
            return log.result
        wait_for_hubs(app, timeout, log)
        data = app.data
        if not selection.everything:
            wanted = set(selection.projects)
            data = select_projects(data, selection.keeps_project)
            log.result['projects_matched'] = sum(view.dataProjects.count for view in collection(data.dataHubs))
            if log.result['projects_matched'] != len(wanted):
                raise ValueError('Only %d of %d requested projects were found'
                                 % (log.result['projects_matched'], len(wanted)))
        if mode == 'inventory':
            listing = inventory(data, log, stop=lambda: (output / 'STOP').exists(), output=output)
            atomic_write(output / 'inventory.json', listing)
            log.result['counts'] = listing['counts']
        else:
            exporter = Exporter(output, FusionAdapter(app), error_callback=log.error)

            def progress(state):
                if state['phase'] == 'design':  # counts only: names stay out of the log
                    log.write('project %d/%d design %d' % (state['project'], state['projects'], state['design']))
                adsk.doEvents()

            try:
                log.result['counts'] = exporter.run(
                    data, cancelled=lambda: (output / 'STOP').exists(), progress=progress, limit=limit,
                    selection=selection)
            finally:
                try:
                    write_report(exporter)
                except OSError as error:
                    log.write('report not written: ' + type(error).__name__)
            info = exporter.run_info
            log.result.update(found=info.get('found'),
                              cancelled=info['cancelled'],
                              stopped_reason=info.get('stopped_reason'), stopped_code=info.get('stopped_code'),
                              low_space=info.get('low_space'), free_bytes=info.get('free_bytes'),
                              discovery_errors=len(info['discovery_errors']),
                              bytes=sum(f['bytes'] for e in exporter.manifest['entries']
                                        if e['status'] == 'done' for f in e['files']))
    except Exception as error:
        if log is not None:
            log.error('autorun', error)
    finally:
        if log is not None:
            log.result['finished'] = now()
            log.result['timings']['total_seconds'] = time.monotonic() - started
            log.write('autorun finished')
            atomic_write(log.output / 'autorun-result.json', log.result)
    return log.result if log else None
