"""Unattended real-application exercise of the normal export pipeline."""
import json
from pathlib import Path
import time
import traceback

import adsk
import adsk.core

from .core import Exporter, NonDesign, atomic_write, collection, now
from .fusion import FusionAdapter


class Recorder:
    def __init__(self, output):
        self.output = output
        self.result = dict(schema=1, started=now(), finished=None, passed=False,
                           fusion_version=None, uploads=[], designs=[], errors=[], timings={})

    def log(self, message):
        with (self.output / 'selftest.log').open('a', encoding='utf-8') as stream:
            stream.write(now() + ' ' + message + '\n')
            stream.flush()

    def error(self, stage, error):
        detail = dict(stage=stage, error=str(error), traceback=traceback.format_exc())
        self.result['errors'].append(detail)
        self.log(stage + ': ' + detail['traceback'])
        return detail


class Only:
    """A one-item API collection."""
    def __init__(self, value):
        self.value = value
        self.count = 1

    def item(self, index):
        if index != 0:
            raise IndexError(index)
        return self.value


def project_scope(hub, project):
    """Data view containing only the self-test project, so other designs are never opened."""
    view = type('HubView', (), {})()
    view.id, view.name, view.dataProjects = hub.id, hub.name, Only(project)
    data = type('DataView', (), {})()
    data.dataHubs = Only(view)
    return data


class ObservedAdapter(FusionAdapter):
    def __init__(self, app, recorder):
        super().__init__(app)
        self.recorder = recorder
        self.observations = {}

    def archive_extension(self, document, data_file):
        extension = super().archive_extension(document, data_file)
        self.observations[str(data_file.id)].update(
            document_references=document.documentReferences.count,
            child_references=data_file.childReferences.count,
            expected_archive=('.f3z' if document.documentReferences.count > 0
                              or data_file.childReferences.count > 0 else '.f3d'))
        return extension

    def export(self, data_file, stem):
        identity = str(data_file.id)
        observation = self.observations.setdefault(identity, dict(attempt_details=[]))
        attempt = dict(started=now(), files=[])
        observation['attempt_details'].append(attempt)
        started = time.monotonic()
        self.recorder.log('export start ' + identity + ' ' + data_file.name)
        try:
            files = super().export(data_file, stem)
            attempt['files'] = files
            observation['archive_correct'] = Path(files[0]).suffix == observation['expected_archive']
            return files
        except Exception as error:
            attempt.update(error=str(error), traceback=traceback.format_exc())
            # The normal pipeline distinguishes unsupported products from export failures.
            if isinstance(error, NonDesign):
                observation['non_design'] = True
            raise
        finally:
            attempt['seconds'] = time.monotonic() - started
            self.recorder.log('export end ' + identity + ' ' + str(attempt['seconds']))


def wait_until(probe, timeout, description, recorder):
    deadline = time.monotonic() + timeout
    heartbeat = time.monotonic() + 10
    recorder.log('waiting: ' + description)
    while True:
        adsk.doEvents()
        value = probe()
        if value:
            recorder.log('ready: ' + description)
            return value
        current = time.monotonic()
        if current >= deadline:
            raise TimeoutError('Timed out waiting for ' + description)
        if current >= heartbeat:
            recorder.log('still waiting: ' + description)
            heartbeat = current + 10
        time.sleep(0.2)


def run_selftest(app, config_path):
    """Never show UI or delete cloud data; return and atomically persist evidence."""
    config_path = Path(config_path)
    recorder = None
    exporter = None
    adapter = None
    started = time.monotonic()
    try:
        # A bad config still gets local diagnostics when its output cannot be read.
        text = config_path.read_text(encoding='utf-8')
        # One-shot: later Fusion launches start normally instead of uploading again.
        config_path.replace(config_path.with_name(config_path.name + '.last'))
        config = json.loads(text)
        output = Path(config['output']).expanduser()
        if not output.is_absolute():
            raise ValueError('output must be an absolute path')
        output.mkdir(parents=True, exist_ok=True)
        recorder = Recorder(output)
        recorder.log('self-test start')
        recorder.result['fusion_version'] = app.version
        seed = Path(config['seed_folder']).expanduser()
        if not seed.is_absolute() or not seed.is_dir():
            raise ValueError('seed_folder must be an existing absolute directory')
        seeds = sorted(p for p in seed.rglob('*') if p.is_file() and p.suffix.lower() in {'.f3d', '.f3z'})
        if not seeds:
            raise ValueError('seed_folder contains no .f3d or .f3z files')
        project_name = config.get('project', 'bulk-export-selftest')
        if not isinstance(project_name, str) or not project_name.strip():
            raise ValueError('project must be a nonempty string')
        scope = config.get('scope', 'project')
        if scope not in ('project', 'all'):
            raise ValueError('scope must be "project" or "all"')
        timeout = float(config.get('upload_timeout_seconds', 600))
        if not 0 < timeout <= 86400:
            raise ValueError('upload_timeout_seconds must be between 0 and 86400')
        hubs = wait_until(lambda: app.data.dataHubs if app.data.dataHubs.count else None,
                          timeout, 'first hub', recorder)
        hub = hubs.item(0)
        projects = hub.dataProjects
        project = next((p for p in collection(projects) if p.name == project_name), None)
        if project is None:
            recorder.log('creating project ' + project_name)
            project = projects.add(project_name)
        if not project:
            raise RuntimeError('Could not create self-test project')
        recorder.result.update(hub=str(hub.id), project=str(project.id), project_name=project_name)
        folder = project.rootFolder
        upload_start = time.monotonic()
        for path in seeds:
            upload = dict(source=str(path), status='failed', started=now())
            recorder.result['uploads'].append(upload)
            item_start = time.monotonic()
            recorder.log('upload start ' + str(path))
            try:
                future = folder.uploadFile(str(path))
                if not future:
                    raise RuntimeError('uploadFile returned no future')

                def uploaded():
                    state = future.uploadState
                    if upload.get('upload_state') != str(state):
                        recorder.log('upload state ' + str(state) + ' ' + str(path))
                    upload['upload_state'] = str(state)
                    if state == adsk.core.UploadStates.UploadFailed:
                        raise RuntimeError('Fusion upload failed: ' + str(path))
                    if state == adsk.core.UploadStates.UploadFinished:
                        if not future.dataFile:
                            raise RuntimeError('Finished upload has no dataFile')
                        return future.dataFile
                    return None

                file = wait_until(uploaded, timeout, 'upload ' + str(path), recorder)
                # Finished transfer can precede cloud translation/reference processing.
                def processed():
                    file.refresh(None)
                    return file.isComplete

                wait_until(processed, timeout, 'cloud processing ' + str(path), recorder)
                upload.update(status='done', id=str(file.id), name=file.name)
            except Exception as error:
                upload.update(recorder.error('upload ' + str(path), error))
            finally:
                upload['seconds'] = time.monotonic() - item_start
                recorder.log('upload end ' + str(path) + ' ' + upload['status'])
        recorder.result['timings']['uploads_seconds'] = time.monotonic() - upload_start
        adapter = ObservedAdapter(app, recorder)
        exporter = Exporter(output, adapter, error_callback=recorder.error)
        export_start = time.monotonic()
        try:
            data = project_scope(hub, project) if scope == 'project' else app.data
            recorder.result['scope'] = scope
            recorder.result['counts'] = exporter.run(
                data, progress=lambda i, total, name: recorder.log('design %d/%d %s' % (i + 1, total, name)))
        finally:
            recorder.result['timings']['export_seconds'] = time.monotonic() - export_start
    except Exception as error:
        if recorder is None:
            output = config_path.parent / 'selftest-output'
            output.mkdir(parents=True, exist_ok=True)
            recorder = Recorder(output)
        recorder.error('self-test', error)
    finally:
        if recorder is not None:
            if exporter is not None:
                for entry in exporter.manifest['entries']:
                    design = dict(entry)
                    observed = adapter.observations.get(entry['id'], {})
                    design.update(observed)
                    design.setdefault('archive_correct', None)
                    design.setdefault('attempt_details', [])
                    design['errors'] = [e for e in recorder.result['errors'] if e['stage'] == entry['key']]
                    recorder.result['designs'].append(design)
                    if design['status'] == 'done' and design['archive_correct'] is not True:
                        try:
                            raise RuntimeError('Archive choice unverified or incorrect: ' + entry['name'])
                        except RuntimeError as error:
                            recorder.error('verification ' + entry['key'], error)
                uploaded_ids = {u.get('id') for u in recorder.result['uploads'] if u['status'] == 'done'}
                done_ids = {d['id'] for d in recorder.result['designs'] if d['status'] == 'done'}
                missing = uploaded_ids - done_ids
                if missing:
                    try:
                        raise RuntimeError('Uploaded designs not exported: ' + ', '.join(sorted(missing)))
                    except RuntimeError as error:
                        recorder.error('verification', error)
                recorder.result['passed'] = (
                    not recorder.result['errors'] and bool(uploaded_ids) and not missing
                    and all(d['status'] == 'done' and d['archive_correct'] is True
                            for d in recorder.result['designs']))
            recorder.result['finished'] = now()
            recorder.result['timings']['total_seconds'] = time.monotonic() - started
            recorder.log('self-test finished passed=' + str(recorder.result['passed']))
            atomic_write(recorder.output / 'selftest-result.json', recorder.result)
    return recorder.result
