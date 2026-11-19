"""Unattended export started from autorun.json: no dialogs, progress in files.

Configuration (all paths absolute):
  {"output": "/path/out",            required; same folder resumes
   "mode": "export" | "inventory",   default "export"
   "projects": ["<project id>", ...],  optional; default every project
   "limit": 25,                      optional; export at most this many designs
   "hub_timeout_seconds": 600}       optional; wait for hubs after sign-in

"inventory" writes inventory.json (every hub, project and design with ids and
versions) without opening anything. Creating a file named STOP in the output
folder ends an export between designs; run again to resume.
"""
import json
from pathlib import Path
import time
import traceback

import adsk
import adsk.core

from .core import Exporter, atomic_write, collection, now, select_projects, walk
from .fusion import FusionAdapter


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
    mode = config.get('mode', 'export')
    if mode not in ('export', 'inventory'):
        raise ValueError('mode must be "export" or "inventory"')
    projects = config.get('projects')
    if projects is not None and (not isinstance(projects, list) or not projects):
        raise ValueError('projects must be a nonempty list of project ids')
    limit = config.get('limit')
    if limit is not None and (not isinstance(limit, int) or limit < 1):
        raise ValueError('limit must be a positive integer')
    timeout = float(config.get('hub_timeout_seconds', 600))
    if not 0 < timeout <= 86400:
        raise ValueError('hub_timeout_seconds must be between 0 and 86400')
    return output, mode, projects, limit, timeout


def wait_for_hubs(app, timeout, log):
    deadline = time.monotonic() + timeout
    while not app.data.dataHubs.count:
        if time.monotonic() >= deadline:
            raise TimeoutError('No hubs appeared; is Fusion signed in?')
        adsk.doEvents()
        time.sleep(0.5)


def inventory(data, log):
    hubs = {}
    errors = []
    for hub, project, path, file in walk(data, lambda where, error: errors.append(dict(path=where, error=str(error)))):
        adsk.doEvents()
        h = hubs.setdefault(str(hub.id), dict(name=hub.name, projects={}))
        p = h['projects'].setdefault(str(project.id), dict(name=project.name, files=[]))
        p['files'].append(dict(id=str(file.id), name=file.name, path=path,
                               extension=file.fileExtension, version=int(file.versionNumber)))
    designs = sum(1 for h in hubs.values() for p in h['projects'].values()
                  for f in p['files'] if f['extension'].lower() == 'f3d')
    files = sum(len(p['files']) for h in hubs.values() for p in h['projects'].values())
    projects = sum(len(h['projects']) for h in hubs.values())
    log.write('inventory: %d hubs, %d projects, %d files, %d designs, %d unreadable'
              % (len(hubs), projects, files, designs, len(errors)))
    return dict(hubs=hubs, unreadable=errors,
                counts=dict(hubs=len(hubs), projects=projects, files=files, designs=designs,
                            unreadable=len(errors)))


def run_autorun(app, config_path):
    config_path = Path(config_path)
    log = None
    started = time.monotonic()
    try:
        try:
            output, mode, projects, limit, timeout = read_config(config_path)
        except Exception:
            output = config_path.parent / 'autorun-output'
            raise
        finally:
            output.mkdir(parents=True, exist_ok=True)
            log = Log(output)
        log.result.update(mode=mode, fusion_version=app.version, projects=projects, limit=limit)
        log.write('autorun start ' + mode)
        wait_for_hubs(app, timeout, log)
        data = app.data
        if projects is not None:
            wanted = set(projects)
            data = select_projects(data, lambda hub, project: str(project.id) in wanted)
            log.result['projects_matched'] = sum(view.dataProjects.count for view in collection(data.dataHubs))
            if log.result['projects_matched'] != len(wanted):
                raise ValueError('Only %d of %d requested projects were found'
                                 % (log.result['projects_matched'], len(wanted)))
        if mode == 'inventory':
            listing = inventory(data, log)
            atomic_write(output / 'inventory.json', listing)
            log.result['counts'] = listing['counts']
        else:
            exporter = Exporter(output, FusionAdapter(app), error_callback=log.error)

            def progress(index, total, name):
                log.write('design %d/%d' % (index + 1, total))
                adsk.doEvents()

            log.result['counts'] = exporter.run(
                data, cancelled=lambda: (output / 'STOP').exists(), progress=progress, limit=limit)
            log.result.update(found=exporter.run_info.get('found'),
                              cancelled=exporter.run_info['cancelled'],
                              discovery_errors=len(exporter.run_info['discovery_errors']),
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
