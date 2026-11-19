"""Portable traversal, naming and resumable export orchestration."""
import hashlib
import json
import os
from pathlib import Path
import re
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat()


def collection(values):
    for index in range(values.count):
        yield values.item(index)


def walk(data, on_error=None):
    """Yield files from every hub, project and folder (including project roots).

    With on_error, a project or folder that cannot be read is reported as
    on_error(where, error) and skipped instead of stopping the walk.
    """
    def guarded(where, produce):
        if on_error is None:
            return list(produce())
        try:
            return list(produce())
        except Exception as error:
            on_error(where, error)
            return []

    def folders(hub, project, folder, path):
        where = [hub.name, project.name] + path
        for file in guarded(where, lambda: collection(folder.dataFiles)):
            yield path, file
        for child in guarded(where, lambda: collection(folder.dataFolders)):
            yield from folders(hub, project, child, path + [child.name])
    for hub in collection(data.dataHubs):
        for project in guarded([hub.name], lambda: collection(hub.dataProjects)):
            root = guarded([hub.name, project.name], lambda: [project.rootFolder])
            for folder in root:
                for path, file in folders(hub, project, folder, []):
                    yield hub, project, path, file


class Listing:
    """A fixed list with the count/item(index) shape of Fusion API collections."""
    def __init__(self, values):
        self.values = list(values)
        self.count = len(self.values)

    def item(self, index):
        return self.values[index]


class _View:
    pass


def select_projects(data, keep):
    """Data view holding only the projects for which keep(hub, project) is true."""
    hubs = []
    for hub in collection(data.dataHubs):
        projects = [project for project in collection(hub.dataProjects) if keep(hub, project)]
        if projects:
            view = _View()
            view.id, view.name, view.dataProjects = hub.id, hub.name, Listing(projects)
            hubs.append(view)
    view = _View()
    view.dataHubs = Listing(hubs)
    return view


def sanitize(value, limit=42):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(value)).strip(' .') or '_'
    if value.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *('COM%d' % i for i in range(1, 10)), *('LPT%d' % i for i in range(1, 10))}:
        value = '_' + value
    # ASCII output also avoids platform Unicode normalization collisions.
    value = value.encode('ascii', 'replace').decode().replace('?', '_')
    return value[:limit].rstrip(' .') or '_'


def windows_length(path):
    return len(str(path).encode('utf-16-le')) // 2


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def file_info(path, root):
    sha = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            sha.update(chunk)
    return {'path': path.relative_to(root).as_posix(), 'sha256': sha.hexdigest(), 'bytes': path.stat().st_size}


def atomic_write(path, value):
    temporary = path.with_suffix('.json.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class Exporter:
    def __init__(self, output, adapter, error_callback=None):
        self.root = Path(output).resolve()
        if windows_length(self.root) > 100:
            raise ValueError('Choose an output folder with a path of at most 100 characters.')
        self.root.mkdir(parents=True, exist_ok=True)
        self.adapter = adapter
        self.error_callback = error_callback
        self.manifest_path = self.root / 'manifest.json'
        self.manifest = {'schema': 1, 'runs': [], 'entries': []}
        if self.manifest_path.exists():
            self.manifest = json.loads(self.manifest_path.read_text(encoding='utf-8'))
            if self.manifest.get('schema') != 1:
                raise ValueError('Unsupported manifest schema')
        self.run_info = {'started': now(), 'finished': None, 'cancelled': False}
        self.manifest['runs'].append(self.run_info)

    def save(self):
        atomic_write(self.manifest_path, self.manifest)

    def log(self, message):
        with (self.root / 'export.log').open('a', encoding='utf-8') as stream:
            stream.write(now() + ' ' + message.replace('\n', ' ') + '\n')

    def valid(self, entry):
        if entry.get('status') != 'done' or len(entry.get('files', [])) != 2:
            return False
        try:
            for expected in entry['files']:
                path = (self.root / expected['path']).resolve()
                if not path.is_relative_to(self.root) or file_info(path, self.root) != expected:
                    return False
            return True
        except (OSError, ValueError):
            return False

    def destination(self, hub, project, folders, file, key):
        def segment(obj):
            return sanitize(obj.name, 12) + '~' + digest(str(obj.id))[:8]
        directory = self.root / segment(hub) / segment(project)
        parent = directory
        for index, folder in enumerate(folders):
            directory /= sanitize(folder, 12) + '~' + digest(json.dumps(folders[:index + 1], ensure_ascii=False))[:8]
        # Keep the whole absolute path below 240 ASCII characters on Windows.
        # The full identity hash prevents case, truncation and sanitizing collisions.
        stem = sanitize(file.name, 12) + '~' + digest(key)[:24] + ' v' + str(int(file.versionNumber))
        if folders and windows_length(directory / (stem + '.step')) > 239:
            directory = parent / ('folders~' + digest(json.dumps(folders, ensure_ascii=False))[:16])
        if windows_length(directory / (stem + '.step')) > 239:
            raise ValueError('Export path exceeds 239 characters; choose a shorter output folder.')
        directory.mkdir(parents=True, exist_ok=True)
        return directory / stem

    def run(self, data, cancelled=lambda: False, progress=lambda index, total, name: None, limit=None):
        counts = dict(done=0, failed=0, skipped=0)
        self.run_info['discovery_errors'] = []

        def unreadable(where, error):
            self.run_info['discovery_errors'].append(dict(path=where, error=str(error)))
            self.log('could not read ' + json.dumps(where, ensure_ascii=False) + ': ' + str(error))
            if self.error_callback:
                self.error_callback('discovery ' + json.dumps(where, ensure_ascii=False), error)
        self.save()
        try:
            self.log('run started')
            items = []
            for item in walk(data, unreadable):
                if cancelled():
                    self.run_info['cancelled'] = True
                    return counts
                items.append(item)
            if limit is not None:
                items = items[:limit]
            self.run_info['found'] = len(items)
            entries = {entry['key']: entry for entry in self.manifest['entries']}
            for index, (hub, project, folders, file) in enumerate(items):
                if cancelled():
                    self.run_info['cancelled'] = True
                    break
                progress(index, len(items), file.name)
                if cancelled():
                    self.run_info['cancelled'] = True
                    break
                key = json.dumps([str(hub.id), str(project.id), str(file.id), int(file.versionNumber)])
                entry = entries.get(key)
                if entry and self.valid(entry):
                    counts['skipped'] += 1
                    self.log('verified ' + key)
                    self.save()
                    continue
                entry = {'key': key, 'hub': hub.name, 'project': project.name, 'path': folders,
                         'name': file.name, 'id': str(file.id), 'version': int(file.versionNumber),
                         'date': file.dateModified, 'files': [], 'status': 'skipped', 'error': None,
                         'attempts': 0}
                # Fusion dateModified is a Unix timestamp, retained without conversion.
                if key in entries:
                    self.manifest['entries'].remove(entries[key])
                self.manifest['entries'].append(entry)
                entries[key] = entry
                if file.fileExtension.lower() != 'f3d':
                    entry['error'] = 'Not a Fusion design: ' + file.fileExtension
                else:
                    for attempt in range(2):
                        entry['attempts'] = attempt + 1
                        try:
                            stem = self.destination(hub, project, folders, file, key)
                            paths = self.adapter.export(file, stem)
                            entry['files'] = [file_info(Path(path), self.root) for path in paths]
                            entry.update(status='done', error=None)
                            break
                        except NonDesign as error:
                            if self.error_callback:
                                self.error_callback(key, error)
                            entry.update(status='skipped', error=str(error))
                            break
                        except Exception as error:
                            if self.error_callback:
                                self.error_callback(key, error)
                            entry.update(status='failed', error=str(error), files=[])
                            self.log('attempt failed ' + key + ': ' + str(error))
                            self.save()
                counts[entry['status']] += 1
                self.log(entry['status'] + ' ' + key)
                self.save()
        except Exception as error:
            self.run_info['error'] = str(error)
            raise
        finally:
            self.run_info.update(finished=now(), counts=counts)
            self.save()
            self.log('run finished ' + json.dumps(counts))
        return counts


class NonDesign(Exception):
    """A file opened successfully but contains no Fusion Design product."""
