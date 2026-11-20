"""Portable traversal, naming and resumable export orchestration."""
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
from datetime import datetime, timezone

from . import checks
from .journal import Journal, replay
from .lock import FolderBusy, FolderLock  # noqa: F401 (FolderBusy: raised by run)
from .names import (NAME_LIMIT, WINDOWS_LIMIT, WINDOWS_NAME_LIMIT, Names, clean, design_suffixes, long_path,
                    too_long, windows_length)
from .power import keep_awake
from .staging import Staging

SCHEMA = 2  # manifest schema; schema-1 folders are read and saved as 2

LOW_SPACE_BYTES = 1 << 30   # warn before a run below this much free space
FULL_BYTES = 100 << 20      # after a failed write, below this the disk counts as full
# This many designs failing one after another (offline, signed out) pauses the run. What counts
# (see Exporter.run): an attempted design that fails, unless it had failed already in an earlier
# run (a broken design, not a sign of trouble now). What resets the count: an item for which
# Fusion answered from the cloud: a design exported or opened (even if it holds no design), an
# other file downloaded. An item already up to date, or skipped without asking Fusion, neither
# counts nor resets: it says nothing about whether Fusion can reach the account.
FAILURES_IN_A_ROW = 5
SAVE_EVERY_ITEMS = 50       # rewrite manifest.json after this many finished items...
SAVE_EVERY_SECONDS = 30     # ...or this long, whichever comes first (the journal has each one)


# Run counts, by what happened to each item this run.
COUNT_KEYS = ('exported', 'up_to_date', 'files', 'files_up_to_date', 'skipped', 'failed')
COUNT_OF_STATUS = {'done': 'exported', 'verified': 'up_to_date', 'skipped': 'skipped', 'failed': 'failed'}
FILES_SAVED = 'files'  # the count of other files (not designs) saved this run
FILES_UP_TO_DATE = 'files_up_to_date'  # other files saved before and still unchanged


def count_key(entry, status):
    """The run count an item adds to: other files count apart from designs."""
    if entry is not None and entry.get('kind') == 'file':
        if status == 'done':
            return FILES_SAVED
        if status == 'verified':
            return FILES_UP_TO_DATE
    return COUNT_OF_STATUS[status]


def new_counts():
    """exported: designs written this run; up_to_date: designs already exported and checked;
    files: other files (PDFs, drawings, uploads) saved this run; files_up_to_date: other files
    saved before and checked; skipped: not saved, with a reason (a file type
    Fusion does not hand out, other files when only designs are wanted, a design without a design
    product); failed: will be tried again on the next run."""
    return dict((key, 0) for key in COUNT_KEYS)


def now():
    return datetime.now(timezone.utc).isoformat()


def collection(values):
    for index in range(values.count):
        yield values.item(index)


class FolderPath(list):
    """Folder names below a project root (a plain list to its users), with .ids: the cloud id
    of each folder, or None where the API object has none."""
    def __init__(self, names=(), ids=None):
        super().__init__(names)
        self.ids = list(ids) if ids is not None else [None] * len(self)

    def child(self, folder):
        identity = getattr(folder, 'id', None)
        return FolderPath(list(self) + [folder.name], self.ids + [None if identity is None else str(identity)])


def walk(data, on_error=None, selection=None, visiting=None):
    """Yield (hub, project, path, file) for files in every hub, project and folder (including
    project roots). path is a FolderPath: a list of folder names that also carries their ids.

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
            visiting(hub, project, path, folder)
        if selection is None or selection.keeps(hub, project, path):
            for file in guarded(where, lambda: collection(folder.dataFiles)):
                yield path, file
        for child in guarded(where, lambda: collection(folder.dataFolders)):
            if selection is None or selection.may_contain(project, path.child(child)):
                yield from folders(hub, project, child, path.child(child))
    for hub in collection(data.dataHubs):
        for project in guarded([hub.name], lambda: collection(hub.dataProjects)):
            root = guarded([hub.name, project.name], lambda: [project.rootFolder])
            for folder in root:
                for path, file in folders(hub, project, folder, FolderPath()):
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


def segment_matches(segment, name, identity):
    """Whether one segment of a chosen folder path names this folder: {"id": ...} matches the
    folder with that cloud id (so same-named siblings stay apart), a string matches by name."""
    if isinstance(segment, dict):
        return identity is not None and segment.get('id') is not None and str(segment['id']) == str(identity)
    return segment == name


def path_matches(segments, path):
    """Whether segments match the first len(segments) folders of path (a FolderPath, or a list of
    names when no ids are known)."""
    ids = getattr(path, 'ids', None) or [None] * len(path)
    if len(segments) > len(path):
        return False
    return all(segment_matches(segment, path[index], ids[index]) for index, segment in enumerate(segments))


class Selection:
    """What to export: everything, or chosen projects, whole or by folder.

    projects maps a project id to None (the whole project) or to a list of folder
    paths, each a list of segments below the project root: a folder name, or {"id": folder id}
    (optionally with "name", which is then only informative), which picks exactly that folder
    even when a sibling has the same name. A chosen folder includes its subfolders. A path
    ending in '' means only the files directly in that folder ([''] is the files at the
    project's root).
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
                if len(path) == len(folder) - 1 and path_matches(folder[:-1], path):
                    return True  # files directly in this folder only
            elif path_matches(folder, path):
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
            target = folder[:-1] if folder and folder[-1] == '' else folder
            if len(path) <= len(target):
                if path_matches(target[:len(path)], path):
                    return True
            elif not (folder and folder[-1] == '') and path_matches(folder, path):
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
    """ASCII-only short name, for the shortened fallback layout and schema-1 folders."""
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(value)).strip(' .') or '_'
    if value.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *('COM%d' % i for i in range(1, 10)), *('LPT%d' % i for i in range(1, 10))}:
        value = '_' + value
    # ASCII output also avoids platform Unicode normalization collisions.
    value = value.encode('ascii', 'replace').decode().replace('?', '_')
    return value[:limit].rstrip(' .') or '_'


# Other files that are saved in another format: a drawing is saved as its PDF.
SAVED_AS = {'f2d': 'pdf'}
EXTENSION = re.compile(r'(?=.*[A-Za-z])[A-Za-z0-9]{1,8}')


def file_name_parts(file):
    """(name, extension) of another file on disk. Fusion's DataFile.name of an uploaded file
    already ends in its real extension (and fileExtension can disagree with it), so the name is
    trusted: "Manual.PDF" stays "Manual.PDF", "notes.scr" stays so even when Fusion calls it htm.
    A name without an extension gets fileExtension; a drawing is saved as a PDF."""
    name = str(file.name)
    declared = str(getattr(file, 'fileExtension', '') or '').strip().strip('.')
    if declared.lower() in SAVED_AS:
        if name.lower().endswith('.' + declared.lower()) and len(name) > len(declared) + 1:
            name = name[:-(len(declared) + 1)]
        return name, SAVED_AS[declared.lower()]
    stem, dot, suffix = name.rpartition('.')
    if dot and stem.strip() and (suffix.lower() == declared.lower() or EXTENSION.fullmatch(suffix)):
        return stem, clean(suffix, 16)
    return name, (clean(declared, 16) if declared else '')


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def file_info(path, root):
    """A file's manifest record: relative path, SHA-256, size and modified time (mtime_ns, the
    fast check on resume). Taken after the file is complete, its time included."""
    sha256 = checks.file_sha256(path)
    stat = os.stat(long_path(path))
    return {'path': path.relative_to(root).as_posix(), 'sha256': sha256, 'bytes': stat.st_size,
            'mtime_ns': stat.st_mtime_ns}


def drop_older_files(manifest):
    """Remove entries of another file's older versions saved where a newer version has been
    saved since (it replaced them on disk). Normally done as the newer one is saved; this
    covers a run that stopped before its manifest was saved."""
    newest = {}
    for entry in manifest['entries']:
        if entry.get('kind') == 'file' and entry.get('status') == 'done':
            for info in entry.get('files') or []:
                key = (entry.get('id'), info.get('path'))
                if key not in newest or entry.get('version', 0) > newest[key].get('version', 0):
                    newest[key] = entry
    keep = set(id(entry) for entry in newest.values())
    manifest['entries'] = [e for e in manifest['entries'] if not (
        e.get('kind') == 'file' and e.get('status') == 'done' and id(e) not in keep
        and any((e.get('id'), info.get('path')) in newest for info in e.get('files') or []))]


def free_bytes(path):
    """Free space on the disk holding path (or its nearest existing parent), or None."""
    path = Path(path)
    for candidate in [path] + list(path.parents):
        try:
            return shutil.disk_usage(long_path(candidate)).free
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


EARLIEST_DATE = 946684800  # 2000-01-01: older "modified" dates are not real ones


def design_time(value, now=None):
    """Fusion's dateModified (a Unix timestamp in seconds) as a file time, or None when it is
    missing or implausible. A value in milliseconds is accepted too."""
    if isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds != seconds:  # NaN
        return None
    if seconds > 1e11:
        seconds /= 1000.0
    latest = (time.time() if now is None else now) + 86400
    return seconds if EARLIEST_DATE <= seconds <= latest else None


def reference_facts(file):
    """has_references and references_out_of_date of a cloud file: DataFile properties
    (hasChildReferences, hasOutofDateChildReferences), read where they are, without listing
    anything. A property this Fusion lacks, or one that cannot be read, is None (unknown)."""
    facts = {}
    for key, member in (('has_references', 'hasChildReferences'),
                        ('references_out_of_date', 'hasOutofDateChildReferences')):
        try:
            value = getattr(file, member, None)
            facts[key] = None if value is None else bool(value)
        except Exception:
            facts[key] = None
    return facts


def link_reason(count):
    """The plain failure reason of a design with links that could not be opened or exported."""
    if count:
        return 'A linked design is missing or cannot be opened (%d link%s).' % (count, '' if count == 1 else 's')
    return 'A linked design is missing or cannot be opened.'


class Trouble(Exception):
    """A problem that stops the whole run (not just one design)."""
    def __init__(self, code, reason, error=None):
        super().__init__(reason)
        self.code, self.reason, self.error = code, reason, error


def atomic_write(path, value):
    temporary = path.with_suffix('.json.tmp')
    with open(long_path(temporary), 'w', encoding='utf-8') as stream:
        # One write: json.dump would call write() for every small piece of a large manifest.
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(long_path(temporary), long_path(path))


class Exporter:
    def __init__(self, output, adapter, error_callback=None):
        self.root = Path(output).resolve()
        try:
            os.makedirs(long_path(self.root), exist_ok=True)
        except OSError as error:
            raise ValueError('The output folder cannot be created: %s (%s)' % (self.root, error.strerror or error))
        if not os.path.isdir(long_path(self.root)):
            raise ValueError('The output folder is not a folder: %s' % self.root)
        self.adapter = adapter
        # Fusion writes to short paths here; the files are then moved to their final names.
        self.staging = Staging(self.root)
        try:
            adapter.staging = self.staging
        except AttributeError:
            pass
        self.error_callback = error_callback
        self.last_error = None
        self.platform = sys.platform
        self.manifest_path = self.root / 'manifest.json'
        self.manifest = {'schema': SCHEMA, 'runs': [], 'entries': []}
        if os.path.exists(long_path(self.manifest_path)):
            with open(long_path(self.manifest_path), encoding='utf-8') as stream:
                self.manifest = json.loads(stream.read())
            if self.manifest.get('schema') not in (1, SCHEMA):
                raise ValueError('Unsupported manifest schema')
            # Schema 1 entries keep their (shortened, hashed) files; new exports use real names.
            self.manifest['schema'] = SCHEMA
        # Items finished after the last full save (the previous run stopped without one).
        self.journal = Journal(self.root / 'manifest.journal')
        self.replayed = replay(self.manifest, self.journal.read())
        drop_older_files(self.manifest)
        self.names = Names()
        # Who may write where: relative path -> ids of the cloud files that hold it (recorded in
        # the manifest, or claimed by an item in this run). A file on disk that no item holds
        # (the user's own, or left by a run that stopped before recording it) is never written over.
        self.holders = {}
        for entry in self.manifest['entries']:
            self.names.remember(entry)
            self.hold(entry)
        self.saves = 0          # full manifest.json writes, for checking the cost
        self.unsaved = 0        # finished items only in the journal so far
        self.saved_at = time.monotonic()
        self.clock = time.monotonic
        self.refreshed = False  # set by valid(): an entry's file times were just recorded
        self.other_files = True
        self.run_info = {'started': now(), 'finished': None, 'cancelled': False}
        self.manifest['runs'].append(self.run_info)

    def save(self):
        """Rewrite manifest.json in full, then empty the journal it now contains."""
        atomic_write(self.manifest_path, self.manifest)
        self.journal.clear()
        self.saves += 1
        self.unsaved = 0
        self.saved_at = self.clock()

    def record(self, entry):
        """A finished item: one journal line now, a full save only every so often."""
        self.journal.append(entry)
        self.unsaved += 1
        if self.unsaved >= SAVE_EVERY_ITEMS or self.clock() - self.saved_at >= SAVE_EVERY_SECONDS:
            self.save()

    def log(self, message):
        try:
            with open(long_path(self.root / 'export.log'), 'a', encoding='utf-8') as stream:
                stream.write(now() + ' ' + message.replace('\n', ' ') + '\n')
        except OSError:
            pass  # the log is a convenience; a missing or full disk is caught where it matters

    def trouble(self, error):
        """A Trouble when error (or the state of the output folder) means no design can be
        written now: disk full, output folder gone or read-only. None for a single bad design."""
        if disk_full(error):
            return Trouble('disk_full', 'The disk is full.', error)
        if not os.path.isdir(long_path(self.root)):
            return Trouble('output_missing', 'The output folder is gone (disconnected drive?): '
                           + str(self.root), error)
        probe = self.root / '.write-check'
        try:
            with open(long_path(probe), 'wb') as stream:
                stream.write(b'x')
            os.unlink(long_path(probe))
        except OSError as problem:
            if disk_full(problem):
                return Trouble('disk_full', 'The disk is full.', error)
            return Trouble('output_unwritable', 'The output folder cannot be written: ' + str(problem), error)
        free = free_bytes(self.root)
        if free is not None and free < FULL_BYTES:
            return Trouble('disk_full', 'The disk is full (%d MB free).' % (free >> 20), error)
        return None

    def valid(self, entry):
        """Whether a finished entry's files are still there unchanged. The fast check: the
        size and modified time recorded at export. A file whose time differs (or an entry from
        before times were recorded) is hashed instead, and on a match its time is recorded, so
        it is hashed only once; self.refreshed then says the entry changed. Full hashing of
        everything is checks.check_folder ("Check this folder")."""
        self.refreshed = False
        if entry.get('status') != 'done' or len(entry.get('files', [])) != (1 if entry.get('kind') == 'file' else 2):
            return False
        try:
            for expected in entry['files']:
                # normpath, not resolve(): no file-system call, so long Windows paths are fine.
                path = Path(os.path.normpath(str(self.root / expected['path'])))
                if not path.is_relative_to(self.root):
                    return False
                stat = os.stat(long_path(path))
                if stat.st_size != expected.get('bytes'):
                    return False
                if expected.get('mtime_ns') == stat.st_mtime_ns:
                    continue
                if checks.file_sha256(path) != expected.get('sha256'):
                    return False
                expected['mtime_ns'] = stat.st_mtime_ns
                self.refreshed = True
            return True
        except (OSError, ValueError):
            return False

    def set_times(self, paths, file):
        """Give exported files the design's modified date, as a file browser shows it. Done
        before the files are recorded, so the recorded time (the fast check on resume) is this
        one. A bad date or a file system that refuses leaves the time of writing."""
        when = design_time(getattr(file, 'dateModified', None))
        if when is None:
            return
        for path in paths:
            try:
                os.utime(long_path(path), (when, when))
            except (OSError, ValueError, OverflowError) as error:
                self.log('could not set a file time: ' + str(error))
                return

    def segments(self, hub, project, folders):
        """[(cleaned name, identity), ...] from the output root to an item's folder."""
        ids = getattr(folders, 'ids', None) or [None] * len(folders)
        limit = self.name_limit
        segments = [(clean(hub.name, limit), 'hub:' + str(hub.id)),
                    (clean(project.name, limit), 'project:' + str(project.id))]
        for index, (name, identity) in enumerate(zip(folders, ids)):
            segments.append((clean(name, limit), 'folder:' + identity if identity is not None else
                             'path:' + json.dumps([str(project.id)] + list(folders[:index + 1]), ensure_ascii=False)))
        return segments

    def short_directory(self, hub, project, folders):
        """The folder of the layout of schema 1: 12 ASCII characters and a hash per name."""
        def segment(obj):
            return sanitize(obj.name, 12) + '~' + digest(str(obj.id))[:8]
        directory = self.root / segment(hub) / segment(project)
        if folders:
            directory /= 'folders~' + digest(json.dumps([str(project.id)] + list(folders), ensure_ascii=False))[:16]
        return directory

    def make_directory(self, directory):
        """True when the folder exists now; False when it cannot be created (the item then goes to
        the shortened layout). Raises when the output folder as a whole is unusable."""
        try:
            os.makedirs(long_path(directory), exist_ok=True)
            return True
        except OSError as error:
            if self.trouble(error) is not None:
                raise
            self.log('cannot create a folder, using the shortened layout: ' + str(error))
            return False

    @property
    def name_limit(self):
        """Characters in one folder or file name. Shorter on Windows, so that typical layouts
        stay under its 260-character paths, which Explorer and many programs still need."""
        return WINDOWS_NAME_LIMIT if self.platform == 'win32' else NAME_LIMIT

    def mark_long_path(self, entry):
        """On Windows, long_path: true on an entry with a file whose full path is over 259
        characters: it is written (with the long-path prefix), but Explorer, many programs and
        Fusion's own Open cannot use it. The report lists such items."""
        entry.pop('long_path', None)
        if self.platform != 'win32':
            return
        infos = list(entry.get('files') or []) + [entry.get('thumbnail') or {}]
        if any(info.get('path') and windows_length(self.root / info['path']) > WINDOWS_LIMIT for info in infos):
            entry['long_path'] = True

    def hold(self, entry, paths=None):
        """Record that entry's cloud file holds these relative paths (default: its recorded files
        and thumbnail)."""
        if paths is None:
            paths = [info.get('path') for info in list(entry.get('files') or []) + [entry.get('thumbnail') or {}]]
        for path in paths:
            if path:
                self.holders.setdefault(path, set()).add(str(entry.get('id')))

    def free(self, location, name, file_id, owned=()):
        """Whether the item with this cloud id may write location/name: nothing is there, or what
        is there is its own (an earlier version, an earlier attempt, its record before this run)."""
        relative = location + '/' + name if location else name
        if relative in owned or str(file_id) in self.holders.get(relative, ()):
            return True
        return not os.path.lexists(long_path(self.root / relative))

    def claim_names(self, segments, base, suffixes, file, owned):
        """(relative directory parts, base) for an item: its folder, and a base name whose
        files (base + each suffix) are free for it in the folder's single namespace of full
        names, wherever they come from (designs, thumbnails, other files). The paths are then
        held for this item, so a second attempt writes them again."""
        parts = self.names.directory(segments)
        location = '/'.join(parts)
        base = self.names.claim_group(location, base, suffixes, 'file:' + str(file.id),
                                      free=lambda name: self.free(location, name, file.id, owned))
        self.hold(dict(id=str(file.id)), [location + '/' + base + suffix for suffix in suffixes])
        return parts, base

    def destination(self, hub, project, folders, file, short=False, owned=()):
        """Where a design goes: (stem path without extension, facts for its manifest entry).

        Real names: <hub>/<project>/<folders...>/<design> v<version>, made portable by
        names.clean, with a "~1a2b" suffix only where two items would share a name. When that
        path is too long for this system, cannot be created, or short is set, the design goes
        to the shortened layout of earlier versions instead, recorded as shortened: true."""
        version = ' v' + str(int(file.versionNumber))
        if not short:
            segments = self.segments(hub, project, folders)
            parts, base = self.claim_names(segments, clean(file.name, self.name_limit),
                                           design_suffixes(file.versionNumber),
                                           file, owned)
            directory = self.root.joinpath(*parts)
            stem = directory / (base + version)
            facts = dict(location='/'.join(parts), identities=[identity for _, identity in segments],
                         base=base, shortened=False)
            if not too_long(str(stem) + '.step', self.platform) and self.make_directory(directory):
                return stem, facts
        directory = self.short_directory(hub, project, folders)
        stem = directory / (sanitize(file.name, 12) + '~' + digest(str(file.id))[:24] + version)
        if too_long(str(stem) + '.step', self.platform):
            raise ValueError('The export path is too long even when shortened; choose a shorter output folder.')
        os.makedirs(long_path(directory), exist_ok=True)
        return stem, dict(location=None, identities=None, base=None, shortened=True)

    def file_destination(self, hub, project, folders, file, owned=()):
        """Where another file (not a design) goes: (path, facts for its manifest entry). It keeps
        its own name and extension, "<name>.<extension>", in the same layout as designs; the
        "~1a2b" suffix, where needed, goes before the extension. A newer version of the file
        replaces the older one at the same path."""
        name, extension = file_name_parts(file)
        suffix = '.' + extension if extension else ''
        identity = 'file:' + str(file.id)
        segments = self.segments(hub, project, folders)
        parts, base = self.claim_names(segments, clean(name, self.name_limit - len(suffix)), [suffix], file, owned)
        location = '/'.join(parts)
        directory = self.root.joinpath(*parts)
        target = directory / (base + suffix)
        facts = dict(location=location, identities=[identity for _, identity in segments], base=base,
                     extension=extension, shortened=False)
        if not too_long(str(target), self.platform) and self.make_directory(directory):
            return target, facts
        directory = self.short_directory(hub, project, folders)
        target = directory / (sanitize(name, 12) + '~' + digest(str(file.id))[:24] + sanitize(suffix, 16))
        if too_long(str(target), self.platform):
            raise ValueError('The export path is too long even when shortened; choose a shorter output folder.')
        os.makedirs(long_path(directory), exist_ok=True)
        return target, dict(location=None, identities=None, base=None, extension=extension, shortened=True)

    def drop_replaced(self, entry, entries):
        """After a newer version of another file was saved over the same path: the older
        versions' entries describe a file that is gone, so they go."""
        paths = {info['path'] for info in entry['files']}
        for old in [e for e in self.manifest['entries'] if e is not entry and e.get('kind') == 'file'
                    and e.get('id') == entry['id'] and {i.get('path') for i in e.get('files') or []} & paths]:
            self.manifest['entries'].remove(old)
            entries.pop(old['key'], None)

    def run(self, data, cancelled=lambda: False, progress=lambda state: None, limit=None, selection=None,
            on_trouble=None, other_files=True):
        """Export as designs are found: each folder is listed, its designs exported, then the next.

        limit: stop once this many designs were exported in this run (designs already up to
        date, failures and other files do not count).

        progress(state) is called while listing and before each item, with a dict:
        phase ('listing' or 'design'), project and projects (position among the chosen projects),
        project_name, path (folder names), design (items found so far: designs and other files),
        name and kind ('design', or 'file' for another file); also
        folder_files (the current folder's file count, or None when not known), folder_item
        (position of this design in that folder), counts (a copy of the run's counts so far),
        elapsed (seconds since the run started) and seconds_per_design (average time of the
        designs opened so far, not counting those already up to date; None before the first).

        When the run cannot go on (disk full, output folder gone, or FAILURES_IN_A_ROW designs
        failing in a row, as when offline or signed out; see FAILURES_IN_A_ROW for what counts),
        on_trouble(reason, last_error) is
        asked: 'retry' carries on (designs failed so far are retried on the next run), anything
        else stops. Without on_trouble it stops. run_info['stopped_reason'] then says why.

        other_files: also save the files that are not designs (PDFs, drawings, uploads) as
        Fusion hands them out; when false they are skipped as "Not a Fusion design".

        The output folder is locked (export.lock) for the run: FolderBusy is raised when another
        export is using it.
        """
        lock = FolderLock(self.root)
        lock.acquire()
        try:
            return self._run(data, cancelled, progress, limit, selection, on_trouble, other_files)
        finally:
            lock.release()

    def _run(self, data, cancelled, progress, limit, selection, on_trouble, other_files):
        self.other_files = bool(other_files)
        self.run_info['other_files'] = self.other_files
        counts = new_counts()
        selection = selection or Selection()
        self.run_info['selection'] = selection.describe()
        if not selection.everything:
            data = select_projects(data, selection.keeps_project)
        self.run_info.update(discovery_errors=[], stopped_reason=None, stopped_code=None,
                             journal_replayed=self.replayed)
        free = free_bytes(self.root)
        self.run_info.update(free_bytes=free, low_space=free is not None and free < LOW_SPACE_BYTES)
        if self.run_info['low_space']:
            self.log('low disk space: %d MB free' % (free >> 20))

        def unreadable(where, error):
            self.run_info['discovery_errors'].append(dict(path=where, error=str(error)))
            self.log('could not read ' + json.dumps(where, ensure_ascii=False) + ': ' + str(error))
            if self.error_callback:
                self.error_callback('discovery ' + json.dumps(where, ensure_ascii=False), error)
        self.staging.clean()  # anything a stopped run left half-written
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
            started = self.clock()
            timing = dict(seconds=0.0, designs=0)
            state = dict(phase='listing', project=0, projects=len(numbers), project_name=None,
                         path=[], design=0, name=None, kind=None, folder_files=None, folder_item=0)

            def report(phase):
                state['phase'] = phase
                snapshot = dict(state, counts=dict(counts), elapsed=self.clock() - started,
                                seconds_per_design=(timing['seconds'] / timing['designs']
                                                    if timing['designs'] else None))
                progress(snapshot)

            def place(hub, project, path):
                state.update(project=numbers.get((str(hub.id), str(project.id)), state['project']),
                             project_name=project.name, path=list(path))

            def visiting(hub, project, path, folder):
                place(hub, project, path)
                try:
                    files = int(folder.dataFiles.count)  # known without listing the files
                except Exception:
                    files = None
                state.update(name=None, folder_files=files, folder_item=0)
                report('listing')

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
                    state.update(design=found, name=file.name, folder_item=state['folder_item'] + 1,
                                 kind='design' if str(file.fileExtension).lower() == 'f3d' else 'file')
                    report('design')
                    if cancelled():
                        self.run_info['cancelled'] = True
                        break
                    began = self.clock()
                    earlier = entries.get(self.item_key(hub, project, file))
                    failed_before = earlier is not None and earlier.get('status') == 'failed'
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
                    if status != 'verified' and str(file.fileExtension).lower() == 'f3d':
                        timing['seconds'] += self.clock() - began
                        timing['designs'] += 1
                    if self.reached_cloud(status, entries.get(self.item_key(hub, project, file))):
                        failing = []
                    elif status == 'failed' and (problem is not None or not failed_before):
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
                    if limit is not None and counts['exported'] >= limit:
                        break  # a small run on a large account need not list everything
        except Exception as error:
            self.run_info['error'] = str(error)
            raise
        finally:
            self.staging.clean()
            self.run_info.update(finished=now(), counts=counts, found=found)
            try:
                self.save()
            except OSError:
                if stopping is None:
                    raise  # with a stop reason, the folder is known to be unusable
            self.log('run finished ' + json.dumps(counts))
        return counts

    @staticmethod
    def item_key(hub, project, file):
        return json.dumps([str(hub.id), str(project.id), str(file.id), int(file.versionNumber)])

    @staticmethod
    def reached_cloud(status, entry):
        """Whether this item shows that Fusion can reach the account now: a design exported or
        opened (a skip as not a design), another file downloaded."""
        if status == 'done':
            return True
        return (status == 'skipped' and entry is not None and entry.get('kind') == 'design'
                and entry.get('attempts', 0) > 0)

    def export_one(self, hub, project, folders, file, entries, counts):
        """Export one design or save one other file; returns its status. Raises Trouble when
        the run must stop."""
        self.last_error = None
        key = self.item_key(hub, project, file)
        kind = 'design' if str(file.fileExtension).lower() == 'f3d' else 'file'
        entry = entries.get(key)
        if entry and self.valid(entry):
            counts[count_key(entry, 'verified')] += 1
            self.log('verified ' + key)
            if self.refreshed:
                self.record(entry)  # its files' times were recorded now; else nothing to save
            return 'verified'
        entry = {'key': key, 'kind': kind, 'hub': hub.name, 'project': project.name, 'path': list(folders),
                 'name': file.name, 'id': str(file.id), 'version': int(file.versionNumber),
                 'date': file.dateModified, 'files': [], 'status': 'skipped', 'error': None,
                 'attempts': 0}
        # Fusion dateModified is a Unix timestamp, retained without conversion.
        owned = set()  # paths this item's earlier record held: it may write over them
        if key in entries:
            owned = {info.get('path') for info in list(entries[key].get('files') or [])
                     + [entries[key].get('thumbnail') or {}] if info.get('path')}
            self.manifest['entries'].remove(entries[key])
        self.manifest['entries'].append(entry)
        entries[key] = entry
        trouble = None
        if kind == 'file':
            if self.other_files and hasattr(self.adapter, 'download'):
                trouble = self.save_file(hub, project, folders, file, entry, entries, owned)
            else:
                entry['error'] = 'Not a Fusion design: ' + file.fileExtension
        else:
            trouble = self.export_design(hub, project, folders, file, entry, key, owned)
        counts[count_key(entry, entry['status'])] += 1
        self.log(entry['status'] + ' ' + key)
        if trouble is not None:
            raise trouble  # the run's final save keeps this entry if the disk still allows
        self.record(entry)
        return entry['status']

    def links(self, file, entry):
        """Record a design's links on its entry: references (the linked designs, by id and name),
        has_references and references_out_of_date. Read from the cloud file in hand, never by
        opening anything; reading them never stops an export (unknown is None)."""
        entry.update(reference_facts(file))
        if hasattr(self.adapter, 'references'):
            try:
                entry['references'] = self.adapter.references(file)
            except Exception as error:
                entry['references'] = None
                self.log('could not list the links of ' + entry['key'] + ': ' + type(error).__name__)
        if entry.get('references') and entry.get('has_references') is None:
            entry['has_references'] = True

    def export_design(self, hub, project, folders, file, entry, key, owned=()):
        """Write a design's .f3d and STEP (two attempts); the Trouble that stops the run, or None.

        A design with links (see links()) that fails to open or export gets the plain reason
        link_reason() in error, and Fusion's own message in fusion_error: the usual cause is a
        linked design that was deleted, moved or cannot be read. The export is attempted as
        for any design; only the reason differs."""
        self.links(file, entry)
        for attempt in range(2):
            entry['attempts'] = attempt + 1
            try:
                stem, facts = self.destination(hub, project, folders, file, owned=owned)
                entry.update(facts)
                written = self.adapter.export(file, stem)
                paths = [path for path in written if not str(path).lower().endswith('.png')]
                pictures = [path for path in written if str(path).lower().endswith('.png')]
                details = dict(getattr(self.adapter, 'details', None) or {})
                # Quick checks now: a broken file is retried; an empty STEP is a warning.
                facts = checks.inline(paths, details.get('visible_solid_bodies'),
                                      details.get('visible_solid_bodies_distinct'))
                # Files get the design's date first, then are recorded with that time.
                self.set_times(paths + pictures, file)
                entry['files'] = [file_info(Path(path), self.root) for path in paths]
                entry.pop('thumbnail', None)
                if pictures:
                    entry['thumbnail'] = file_info(Path(pictures[0]), self.root)
                entry.update(details)
                entry.update(facts)
                entry.update(status='done', error=None)
                entry.pop('fusion_error', None)
                self.mark_long_path(entry)
                return None
            except NonDesign as error:
                if self.error_callback:
                    self.error_callback(key, error)
                entry.update(status='skipped', error=str(error))
                entry.pop('fusion_error', None)
                return None
            except Exception as error:
                if self.error_callback:
                    self.error_callback(key, error)
                entry.update(status='failed', error=str(error), files=[])
                entry.pop('fusion_error', None)
                self.last_error = str(error)
                self.log('attempt failed ' + key + ': ' + str(error))
                trouble = self.trouble(error)
                if trouble is not None:
                    return trouble  # a second attempt cannot succeed either
                if entry.get('has_references') and not isinstance(error, (OpenInFusion, ValueError)):
                    entry.update(error=link_reason(len(entry.get('references') or [])), fusion_error=str(error))
        return None

    def save_file(self, hub, project, folders, file, entry, entries, owned=()):
        """Save another file (PDF, drawing, upload) as Fusion hands it out, under its own name.
        Anything Fusion does not hand out is a skip with the reason (it is tried again on the next
        run); only a full disk or a lost output folder is a failure. Returns the Trouble that
        stops the run, or None."""
        entry['attempts'] = 1
        try:
            target, facts = self.file_destination(hub, project, folders, file, owned)
            entry.update(facts)
            path = Path(self.adapter.download(file, target))
            self.set_times([path], file)
            entry['files'] = [file_info(path, self.root)]
            entry.update(status='done', error=None)
            self.mark_long_path(entry)
            self.drop_replaced(entry, entries)
            return None
        except NotSaved as error:
            entry.update(status='skipped', error=str(error), files=[])
            return None
        except Exception as error:
            if self.error_callback:
                self.error_callback(entry['key'], error)
            self.log('saving failed ' + entry['key'] + ': ' + str(error))
            trouble = self.trouble(error)
            if trouble is not None:
                entry.update(status='failed', error=str(error), files=[])
                self.last_error = str(error)
                return trouble
            entry.update(status='skipped', error='Not saved: ' + str(error), files=[])
            return None


class NonDesign(Exception):
    """A file opened successfully but contains no Fusion Design product."""


class OpenInFusion(RuntimeError):
    """The design (or drawing) is open in Fusion in a way that stops its export (unsaved edits,
    another version); the message says what to do. Not a link problem."""


class NotSaved(Exception):
    """Fusion did not hand out another file (not a design); the message says why."""
