"""Unattended export started from autorun.json: no dialogs, progress in files.

Configuration (all paths absolute):
  {"output": "/path/out",            required; same folder resumes
   "mode": "export" | "inventory" | "diagnostics",   default "export"
   "projects": ["<project id>", ...],  optional; whole projects (default: everything)
   "folders": {"<project id>": ["Folder/Subfolder", {"id": "<folder id>"}, ...]},
                                     optional; chosen folders, by name path or by id
                                     (inventory.json lists each file's folder_ids); a folder
                                     by id is exactly that folder, even beside a same-named one
   "designs_only": false,            optional; true: skip other files (PDFs, uploads),
                                     which are otherwise saved as Fusion hands them out
   "step": true,                     optional; false: each design's .f3d only, no STEP
                                     (entries record step: "off"; a later job with STEP adds
                                     them by exporting those designs again)
   "keep_latest_only": false,        optional; true: once a design's newest version is saved
                                     and checked, the files this export wrote for its older
                                     versions are removed (only unchanged ones; entries stay,
                                     status "removed"). Never set it unless the person asks
   "experiment_linked_copies": "1",  testing only, not for users: the launcher passes on
                                     FUSION_BULK_EXPORT_EXPERIMENT_LINKED_COPIES ("1",
                                     "innermost" or "compute"; see core.LINKED_COPIES_ENV).
                                     Absent: Fusion's own environment decides (off by default)
   "linked_copies": ...,             accepted and ignored (from test builds before the
                                     copies with linked parts became an experiment)
   "drawings": ...,                  accepted and ignored: drawings are never exported
                                     (exporting one crashed Fusion); they are listed as
                                     not supported
   "limit": 25,                      optional; stop after exporting this many designs (those
                                     already up to date and other files do not count); other
                                     files in the folders visited until then are saved, including
                                     the rest of the folder where the limit was reached
   "hub_timeout_seconds": 600,       optional; wait for hubs after sign-in
   "memory_guard": false}            optional; false switches the memory guard off for this
                                     job (it stops a run only when Fusion itself holds too
                                     much memory for a while; see core.py)

"diagnostics" writes diagnostics.json (Fusion version, the export-related API
members and text commands this build offers) for bug reports; it reads no designs.
"inventory" writes inventory.json (every hub, project and design with ids and
versions; for designs also has_references and references_out_of_date, true, false or null when
Fusion does not say, and references_unreadable with the error when Fusion raised on reading
them) without opening anything. An export also writes index.html and index.csv (the
human-readable report) into the output folder; counts in autorun-result.json are exported,
up_to_date (designs), files (other files saved), files_up_to_date, skipped and failed. Creating a file named STOP in the output
folder ends an export between designs; run again to resume.

A job that arrives while an export from the toolbar button is running is not started: an
autorun-result.json in autorun-output beside the add-in (never in the job's output folder, which
may be the running export's) says busy (true) with the reason, and the command can simply be run
again.
"""
import json
from pathlib import Path
import re
import time
import traceback

import adsk
import adsk.core

from .core import (LINK_RECIPES, Exporter, Listing, Selection, atomic_write, collection, link_recipe, now,
                   reference_facts, select_projects, walk)
from .fusion import FusionAdapter
from .report import memory_line, write as write_report
from .words import plural


class Log:
    def __init__(self, output):
        self.output = output
        self.result = dict(schema=1, started=now(), finished=None, mode=None, fusion_version=None,
                           errors=[], timings={})

    def write(self, message):
        try:
            with (self.output / 'autorun.log').open('a', encoding='utf-8') as stream:
                stream.write(now() + ' ' + message + '\n')
        except OSError:
            pass  # a progress line; the result itself always finds a place (run_autorun)

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
                                or not all(isinstance(v, list) and v for v in folders.values())
                                or not all(isinstance(item, str) or (isinstance(item, dict) and set(item) == {'id'}
                                                                     and isinstance(item['id'], str) and item['id'])
                                           for v in folders.values() for item in v)):
        raise ValueError('folders must map project ids to nonempty lists of folder paths or {"id": folder id}')
    names = None if folders is None else {project: [item for item in items if isinstance(item, str)]
                                          for project, items in folders.items()}
    selection = Selection.from_config(projects, names)
    # Folders given by id are turned into name paths once the account can be read.
    selection.folder_ids = {} if folders is None else {
        str(project): [item['id'] for item in items if isinstance(item, dict)]
        for project, items in folders.items() if any(isinstance(item, dict) for item in items)}
    limit = config.get('limit')
    if limit is not None and (not isinstance(limit, int) or limit < 1):
        raise ValueError('limit must be a positive integer')
    timeout = float(config.get('hub_timeout_seconds', 600))
    if not 0 < timeout <= 86400:
        raise ValueError('hub_timeout_seconds must be between 0 and 86400')
    designs_only = config.get('designs_only', False)
    if not isinstance(designs_only, bool):
        raise ValueError('designs_only must be true or false')
    step = config.get('step', True)
    if not isinstance(step, bool):
        raise ValueError('step must be true or false')
    keep_latest_only = config.get('keep_latest_only', False)
    if not isinstance(keep_latest_only, bool):
        raise ValueError('keep_latest_only must be true or false')
    linked_copies = config.get('experiment_linked_copies')
    if linked_copies is not None and link_recipe(linked_copies) is None:
        raise ValueError('experiment_linked_copies must be one of: ' + ', '.join(sorted(LINK_RECIPES)))
    memory_guard = config.get('memory_guard', True)
    if not isinstance(memory_guard, bool):
        raise ValueError('memory_guard must be true or false')
    # "drawings" (from launchers before drawing export was removed) and "linked_copies" are ignored.
    return mode, selection, limit, timeout, not designs_only, linked_copies, step, keep_latest_only, memory_guard


FIND_LOG_EVERY = 50  # folders listed between progress lines while a folder id is looked for


def find_folder(folder, identity, path=(), listed=None):
    """The path below folder to the subfolder with this id, or None: a segment per folder,
    {"id": ..., "name": ...} where the folder has an id (so the path picks exactly that folder,
    even beside a same-named sibling), else its name.

    Breadth first, so a folder near the top is found without listing the deep ones; Fusion is
    let work (adsk.doEvents) after each listing, which can be slow. listed(count), when given,
    is called with the number of folders listed so far."""
    queue = [(folder, list(path))]
    count = 0
    while queue:
        parent, where = queue.pop(0)
        children = list(collection(parent.dataFolders))
        count += 1
        adsk.doEvents()
        if listed is not None:
            listed(count)
        for child in children:
            child_id = getattr(child, 'id', None)
            segment = child.name if child_id is None else dict(id=str(child_id), name=child.name)
            if child_id is not None and str(child_id) == identity:
                return where + [segment]
            queue.append((child, where + [segment]))
    return None


def resolve_folder_ids(data, selection, log=None):
    """Add the path of every folder chosen by id, by ids from the project root down: exactly that
    folder, whatever its name (a name may hold "/", and a sibling may have the same name).
    With log, a counts-only line every FIND_LOG_EVERY folders listed."""
    wanted = getattr(selection, 'folder_ids', None) or {}
    if not wanted:
        return
    projects = {str(project.id): project for hub in collection(data.dataHubs) for project in collection(hub.dataProjects)}
    for project_id, identities in wanted.items():
        if project_id not in projects:
            raise ValueError('Project %s was not found' % project_id)
        if selection.projects.get(project_id, []) is None:
            continue  # the whole project is chosen anyway
        for identity in identities:
            def listed(count):
                if log is not None and count % FIND_LOG_EVERY == 0:
                    log.write('finding folder: %d folders listed' % count)
            path = find_folder(projects[project_id].rootFolder, identity, listed=listed)
            if path is None:
                raise ValueError('Folder id %s was not found in project %s' % (identity, project_id))
            selection.projects.setdefault(project_id, []).append(path)


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
                                links_out_of_date=sum(1 for f in files if f.get('references_out_of_date')),
                                links_unreadable=sum(1 for f in files if f.get('references_unreadable')),
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
                item = dict(id=str(file.id), name=file.name, path=path, folder_ids=path.ids,
                            extension=file.fileExtension, version=int(file.versionNumber))
                if str(file.fileExtension).lower() == 'f3d':
                    problems = []
                    item.update(reference_facts(file, problems))  # properties of the file in hand: nothing is listed
                    if problems:
                        item.update(references_unreadable=True, references_error=problems[0])
                p['files'].append(item)
            log.write('inventory hub %d/%d project %d/%d: %s'
                      % (hub_number, len(hub_list), project_number, len(projects), plural(len(p['files']), 'file')))
            if output is not None:
                atomic_write(output / 'inventory.json', summary())
        if stopped:
            log.write('inventory stopped')
            break
    listing = summary()
    counts = listing['counts']
    log.write('inventory: %s, %s, %s, %s, %d unreadable'
              % (plural(counts['hubs'], 'hub'), plural(counts['projects'], 'project'), plural(counts['files'], 'file'),
                 plural(counts['designs'], 'design'), counts['unreadable']))
    return listing


def _one_project(hub, project):
    """Data view holding just this project, without listing the hub's projects again."""
    hub_view = type('HubView', (), {})()
    hub_view.id, hub_view.name, hub_view.dataProjects = hub.id, hub.name, Listing([project])
    view = type('DataView', (), {})()
    view.dataHubs = Listing([hub_view])
    return view


def open_document_facts(app):
    """open_documents: how many documents are open in Fusion now; open_documents_with_work: how
    many of them hold a cloud file or unsaved changes (anything but an empty, unsaved "Untitled").
    A document whose state cannot be read counts as one with work; when the documents cannot be
    listed at all, both are None (unknown: the launcher then never closes Fusion)."""
    try:
        total = work = 0
        for document in collection(app.documents):
            total += 1
            try:
                cloud = document.dataFile is not None
            except Exception:
                try:
                    cloud = bool(document.isSaved)  # saved once: it has a cloud file
                except Exception:
                    cloud = True
            try:
                modified = bool(document.isModified)
            except Exception:
                modified = True
            if cloud or modified:
                work += 1
        return dict(open_documents=total, open_documents_with_work=work)
    except Exception:
        return dict(open_documents=None, open_documents_with_work=None)


BUSY = ('An export started from the toolbar button is running in Fusion. '
        'Run this command again when it has finished.')


def reject_busy(app, config_path, reason=BUSY):
    """Answer a job that cannot start now: consume autorun.json and write a "busy" result into
    autorun-output beside the add-in, where the launcher looks for it. Never into the job's output
    folder: that may be the folder the running export writes to."""
    config_path = Path(config_path)
    output = config_path.parent / 'autorun-output'
    try:
        config_path.replace(config_path.with_name(config_path.name + '.last'))
    except OSError:
        pass  # gone already
    output.mkdir(parents=True, exist_ok=True)
    log = Log(output)
    log.result.update(busy=True, finished=now(), fusion_version=getattr(app, 'version', None),
                      errors=[dict(stage='busy', error=reason, traceback='')])
    log.write('autorun busy')
    atomic_write(output / 'autorun-result.json', log.result)
    return log.result


def usable_folder(output):
    """None when the job's output folder exists or could be created; else one plain line saying
    Fusion cannot use it (a drive letter the launcher sees but Fusion's session does not, a
    folder it may not create)."""
    try:
        output.mkdir(parents=True, exist_ok=True)
        if output.is_dir():
            return None
        problem = 'it is not a folder'
    except OSError as error:
        problem = error.strerror or type(error).__name__
    return ('Fusion cannot use the output folder %s (%s). A network drive must be connected for Fusion '
            'too; or choose another folder.' % (output, problem))


def run_autorun(app, config_path):
    """Run the job in config_path. Every job taken gets an autorun-result.json the launcher finds:
    in the output folder, or in autorun-output beside the add-in when the job cannot be read or
    the output folder cannot be used."""
    config_path = Path(config_path)
    beside = config_path.parent / 'autorun-output'
    log = None
    started = time.monotonic()
    try:
        unusable = None
        try:
            (output, mode, selection, limit, timeout, other_files, linked_copies, step, keep_latest_only,
             memory_guard) = read_config(config_path)
        except Exception as error:
            output = getattr(error, 'output', None) or beside
            raise
        finally:
            unusable = usable_folder(output)
            if unusable is not None:
                output = beside
                beside.mkdir(parents=True, exist_ok=True)
            log = Log(output)
        if unusable is not None:
            raise ValueError(unusable)
        log.result.update(mode=mode, fusion_version=app.version, selection=selection.describe(), limit=limit,
                          other_files=other_files, step=step, keep_latest_only=keep_latest_only)
        log.write('autorun start ' + mode)
        if mode == 'diagnostics':
            report = diagnostics(app)
            atomic_write(output / 'diagnostics.json', report)
            log.result['counts'] = dict(classes=sum(1 for v in report['classes'].values() if v is not None),
                                        text_commands=len(report['text_commands']))
            return log.result
        wait_for_hubs(app, timeout, log)
        data = app.data
        resolve_folder_ids(data, selection, log)
        log.result['selection'] = selection.describe()
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
            exporter.memory_guard = memory_guard

            def progress(state):
                if state['phase'] == 'design':  # counts only: names stay out of the log
                    log.write('project %d/%d design %d' % (state['project'], state['projects'], state['design']))
                adsk.doEvents()

            try:
                log.result['counts'] = exporter.run(
                    data, cancelled=lambda: (output / 'STOP').exists(), progress=progress, limit=limit,
                    selection=selection, other_files=other_files, linked_copies=linked_copies, step=step,
                    keep_latest_only=keep_latest_only)
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
                                        if e['status'] == 'done' for f in e['files']),
                              memory=info.get('memory'), gc=info.get('gc'), memory_rule=info.get('memory_rule'),
                              memory_guard=info.get('memory_guard', 'on'), low_memory=bool(info.get('low_memory')))
            if info.get('removed'):
                log.result['removed'] = info['removed']
            if info.get('experiment_linked_copies'):
                log.result['experiment_linked_copies'] = info['experiment_linked_copies']
            # For the launcher's --restart-fusion: it closes Fusion only when nothing is open in it
            # that someone could be working on.
            log.result.update(open_document_facts(app))
            line = memory_line(info)
            if line:
                log.write(line)
    except Exception as error:
        if log is not None:
            log.error('autorun', error)
            if unusable is not None:  # the line says it all; the traceback adds nothing
                log.result['errors'][-1].update(error=unusable, traceback='')
    finally:
        if log is not None:
            log.result['finished'] = now()
            log.result['timings']['total_seconds'] = time.monotonic() - started
            log.write('autorun finished')
            try:
                atomic_write(log.output / 'autorun-result.json', log.result)
            except OSError as error:
                if log.output == beside:
                    raise
                # The output folder went away or became unwritable: answer beside the add-in.
                log.result['errors'].append(dict(stage='autorun', traceback='', error=(
                    'The result could not be written in the output folder %s (%s).'
                    % (log.output, error.strerror or type(error).__name__))))
                beside.mkdir(parents=True, exist_ok=True)
                atomic_write(beside / 'autorun-result.json', log.result)
    return log.result if log else None
