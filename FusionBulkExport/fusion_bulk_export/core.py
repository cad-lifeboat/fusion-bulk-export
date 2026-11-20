"""Portable traversal, naming and resumable export orchestration."""
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from datetime import datetime, timezone

from .power import keep_awake

LOW_SPACE_BYTES = 1 << 30   # warn before a run below this much free space
FULL_BYTES = 100 << 20      # after a failed write, below this the disk counts as full
FAILURES_IN_A_ROW = 5       # this many designs failing one after another means something is wrong


def now():
    return datetime.now(timezone.utc).isoformat()


def collection(values):
    for index in range(values.count):
        yield values.item(index)


def walk(data, on_error=None, selection=None, visiting=None):
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
        if visiting:
            visiting(hub, project, path)
        if selection is None or selection.keeps(hub, project, path):
            for file in guarded(where, lambda: collection(folder.dataFiles)):
                yield path, file
        for child in guarded(where, lambda: collection(folder.dataFolders)):
            if selection is None or selection.may_contain(project, path + [child.name]):
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


class Selection:
    """What to export: everything, or chosen projects, whole or by folder.

    projects maps a project id to None (the whole project) or to a list of folder
    paths, each a list of folder names below the project root. A chosen folder
    includes its subfolders. A path ending in '' means only the files directly in
    that folder ([''] is the files at the project's root).
    """
    def __init__(self, projects=None):
        self.projects = None if projects is None else {
            str(key): None if paths is None else [list(path) for path in paths]
            for key, paths in projects.items()}

    @property
    def everything(self):
        return self.projects is None

    def keeps_project(self, hub, project):
        return self.everything or str(project.id) in self.projects

    def keeps(self, hub, project, path):
        if self.everything:
            return True
        paths = self.projects.get(str(project.id), [])
        if paths is None:
            return True
        for folder in paths:
            if folder and folder[-1] == '':
                if list(path) == folder[:-1]:  # files directly in this folder only
                    return True
            elif list(path[:len(folder)]) == folder:
                return True
        return False

    def may_contain(self, project, path):
        """Whether the folder at path is chosen, inside a chosen folder, or above one."""
        if self.everything:
            return True
        paths = self.projects.get(str(project.id), [])
        if paths is None:
            return True
        for folder in paths:
            if folder and folder[-1] == '':
                if folder[:-1][:len(path)] == list(path):
                    return True
            elif list(path[:len(folder)]) == folder or folder[:len(path)] == list(path):
                return True
        return False

    def describe(self):
        if self.everything:
            return 'everything'
        folders = sum(len(paths) for paths in self.projects.values() if paths is not None)
        whole = sum(1 for paths in self.projects.values() if paths is None)
        return '%d whole project(s), %d folder(s)' % (whole, folders)

    @classmethod
    def from_config(cls, projects=None, folders=None):
        """From autorun settings: project ids, and {project id: ["A/B", ...]}."""
        if projects is None and not folders:
            return cls()
        chosen = {str(project): None for project in projects or []}
        for project, paths in (folders or {}).items():
            if chosen.get(str(project), []) is None:
                continue  # whole project already chosen
            chosen.setdefault(str(project), [])
            for path in paths:
                parts = [part for part in str(path).split('/') if part]
                if not parts:
                    raise ValueError('folder paths must name at least one folder')
                chosen[str(project)].append(parts)
        return cls(chosen)


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


def free_bytes(path):
    """Free space on the disk holding path (or its nearest existing parent), or None."""
    path = Path(path)
    for candidate in [path] + list(path.parents):
        try:
            return shutil.disk_usage(str(candidate)).free
        except OSError:
            continue
    return None


DISK_FULL_TEXT = re.compile(r'no space left|disk (is )?full|not enough (disk )?space|insufficient (disk )?space',
                            re.IGNORECASE)


def disk_full(error):
    if isinstance(error, OSError) and (error.errno in (errno.ENOSPC, getattr(errno, 'EDQUOT', -1))
                                       or getattr(error, 'winerror', None) in (39, 112)):
        return True
    return bool(DISK_FULL_TEXT.search(str(error)))


class Trouble(Exception):
    """A problem that stops the whole run (not just one design)."""
    def __init__(self, code, reason, error=None):
        super().__init__(reason)
        self.code, self.reason, self.error = code, reason, error


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
        self.last_error = None
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
        try:
            with (self.root / 'export.log').open('a', encoding='utf-8') as stream:
                stream.write(now() + ' ' + message.replace('\n', ' ') + '\n')
        except OSError:
            pass  # the log is a convenience; a missing or full disk is caught where it matters

    def trouble(self, error):
        """A Trouble when error (or the state of the output folder) means no design can be
        written now: disk full, output folder gone or read-only. None for a single bad design."""
        if disk_full(error):
            return Trouble('disk_full', 'The disk is full.', error)
        if not self.root.is_dir():
            return Trouble('output_missing', 'The output folder is gone (disconnected drive?): '
                           + str(self.root), error)
        probe = self.root / '.write-check'
        try:
            probe.write_bytes(b'x')
            probe.unlink()
        except OSError as problem:
            if disk_full(problem):
                return Trouble('disk_full', 'The disk is full.', error)
            return Trouble('output_unwritable', 'The output folder cannot be written: ' + str(problem), error)
        free = free_bytes(self.root)
        if free is not None and free < FULL_BYTES:
            return Trouble('disk_full', 'The disk is full (%d MB free).' % (free >> 20), error)
        return None

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

    def run(self, data, cancelled=lambda: False, progress=lambda state: None, limit=None, selection=None,
            on_trouble=None):
        """Export as designs are found: each folder is listed, its designs exported, then the next.

        progress(state) is called while listing and before each design, with a dict:
        phase ('listing' or 'design'), project and projects (position among the chosen projects),
        project_name, path (folder names), design (designs found so far) and name.

        When the run cannot go on (disk full, output folder gone, or FAILURES_IN_A_ROW designs
        failing in a row, as when offline or signed out), on_trouble(reason, last_error) is
        asked: 'retry' carries on (designs failed so far are retried on the next run), anything
        else stops. Without on_trouble it stops. run_info['stopped_reason'] then says why.
        """
        counts = dict(done=0, failed=0, skipped=0)
        selection = selection or Selection()
        self.run_info['selection'] = selection.describe()
        if not selection.everything:
            data = select_projects(data, selection.keeps_project)
        self.run_info.update(discovery_errors=[], stopped_reason=None, stopped_code=None)
        free = free_bytes(self.root)
        self.run_info.update(free_bytes=free, low_space=free is not None and free < LOW_SPACE_BYTES)
        if self.run_info['low_space']:
            self.log('low disk space: %d MB free' % (free >> 20))

        def unreadable(where, error):
            self.run_info['discovery_errors'].append(dict(path=where, error=str(error)))
            self.log('could not read ' + json.dumps(where, ensure_ascii=False) + ': ' + str(error))
            if self.error_callback:
                self.error_callback('discovery ' + json.dumps(where, ensure_ascii=False), error)
        self.save()
        found = 0
        failing = []  # errors of the designs that failed one after another
        stopping = None
        try:
            self.log('run started')
            numbers = {}
            for hub in collection(data.dataHubs):
                try:
                    for project in collection(hub.dataProjects):
                        numbers[(str(hub.id), str(project.id))] = len(numbers) + 1
                except Exception:
                    pass  # reported by the walk below
            state = dict(phase='listing', project=0, projects=len(numbers), project_name=None,
                         path=[], design=0, name=None)

            def place(hub, project, path):
                state.update(project=numbers.get((str(hub.id), str(project.id)), state['project']),
                             project_name=project.name, path=list(path))

            def visiting(hub, project, path):
                place(hub, project, path)
                state.update(phase='listing', name=None)
                progress(dict(state))

            entries = {entry['key']: entry for entry in self.manifest['entries']}
            with keep_awake():
                for hub, project, folders, file in walk(data, unreadable, None if selection.everything else selection,
                                                        visiting):
                    if cancelled():
                        self.run_info['cancelled'] = True
                        break
                    if not selection.keeps(hub, project, folders):
                        continue
                    found += 1
                    place(hub, project, folders)
                    state.update(phase='design', design=found, name=file.name)
                    progress(dict(state))
                    if cancelled():
                        self.run_info['cancelled'] = True
                        break
                    try:
                        status = self.export_one(hub, project, folders, file, entries, counts)
                        problem = None
                    except Trouble as error:
                        status, problem = 'failed', error
                    except OSError as error:  # writing the manifest or log
                        problem = self.trouble(error)
                        if problem is None:
                            raise
                        status = 'failed'
                    if status == 'done':
                        failing = []
                    elif status == 'failed':
                        failing.append(str(problem.error if problem else self.last_error))
                        if problem is None and len(failing) >= FAILURES_IN_A_ROW:
                            problem = Trouble('failing', '%d designs in a row failed (offline or signed out?).'
                                              % len(failing), failing[-1])
                    if problem is not None:
                        last = str(problem.error) if problem.error is not None else ''
                        self.log('trouble: ' + problem.reason + ' ' + last)
                        answer = on_trouble(problem.reason, last) if on_trouble else 'stop'
                        if answer != 'retry':
                            stopping = problem
                            self.run_info.update(stopped_reason=problem.reason, stopped_code=problem.code)
                            break
                        failing = []
                    if limit is not None and found >= limit:
                        break  # a small run on a large account need not list everything
        except Exception as error:
            self.run_info['error'] = str(error)
            raise
        finally:
            self.run_info.update(finished=now(), counts=counts, found=found)
            try:
                self.save()
            except OSError:
                if stopping is None:
                    raise  # with a stop reason, the folder is known to be unusable
            self.log('run finished ' + json.dumps(counts))
        return counts

    def export_one(self, hub, project, folders, file, entries, counts):
        """Export one file; returns its status. Raises Trouble when the run must stop."""
        self.last_error = None
        key = json.dumps([str(hub.id), str(project.id), str(file.id), int(file.versionNumber)])
        entry = entries.get(key)
        if entry and self.valid(entry):
            counts['skipped'] += 1
            self.log('verified ' + key)
            self.save()
            return 'verified'
        entry = {'key': key, 'hub': hub.name, 'project': project.name, 'path': folders,
                 'name': file.name, 'id': str(file.id), 'version': int(file.versionNumber),
                 'date': file.dateModified, 'files': [], 'status': 'skipped', 'error': None,
                 'attempts': 0}
        # Fusion dateModified is a Unix timestamp, retained without conversion.
        if key in entries:
            self.manifest['entries'].remove(entries[key])
        self.manifest['entries'].append(entry)
        entries[key] = entry
        trouble = None
        if file.fileExtension.lower() != 'f3d':
            entry['error'] = 'Not a Fusion design: ' + file.fileExtension
        else:
            for attempt in range(2):
                entry['attempts'] = attempt + 1
                try:
                    if hasattr(self.adapter, 'references'):
                        entry['references'] = self.adapter.references(file)
                    stem = self.destination(hub, project, folders, file, key)
                    paths = self.adapter.export(file, stem)
                    entry['files'] = [file_info(Path(path), self.root) for path in paths]
                    entry.update(getattr(self.adapter, 'details', None) or {})
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
                    self.last_error = str(error)
                    self.log('attempt failed ' + key + ': ' + str(error))
                    trouble = self.trouble(error)
                    if trouble is not None:
                        break  # a second attempt cannot succeed either
                    self.save()
        counts[entry['status']] += 1
        self.log(entry['status'] + ' ' + key)
        if trouble is not None:
            raise trouble
        self.save()
        return entry['status']


class NonDesign(Exception):
    """A file opened successfully but contains no Fusion Design product."""
