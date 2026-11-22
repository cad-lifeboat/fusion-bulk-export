"""Portable traversal, naming and resumable export orchestration."""
import errno
import gc
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from stat import S_ISREG
import sys
import time
import traceback
from datetime import datetime, timezone

from . import checks, memory
from .journal import Journal, replay
from .lock import FolderBusy, FolderLock  # noqa: F401 (FolderBusy: raised by run)
from .names import (LINKED_SUFFIX, NAME_LIMIT, OLDER_FOLDER, WINDOWS_LIMIT, WINDOWS_NAME_LIMIT, Names, absolute_path,
                    clean, design_suffixes, long_path, output_root_problem, too_long, windows_length)
from .power import inside_fusion, keep_awake
from .staging import Staging
from .words import plural

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
# An item that takes longer than SLOW_SECONDS (Fusion can block for minutes opening a design, as
# for one "locked in another team") gets slow_seconds on its entry and is listed in the report. A
# design whose failed attempt took longer than NO_RETRY_SECONDS is not tried a second time in the
# run: that would block Fusion as long again (the next run tries it again).
SLOW_SECONDS = 60
NO_RETRY_SECONDS = 120
SAVE_EVERY_ITEMS = 50       # rewrite manifest.json after this many finished items...
SAVE_EVERY_SECONDS = 30     # ...or this long, whichever comes first (the journal has each one)
# After each item Fusion worked on (not one found up to date), Python's cycle collector runs once
# (Exporter.collect): a Fusion API object (document, design, body) caught in a reference cycle is
# then released at once instead of whenever Python next gets round to it. Each item gets a young
# collection (what that item made); every FULL_COLLECT_EVERY items a full one, whose cost grows
# with the manifest held in memory (tens of milliseconds at thousands of items, against seconds
# per design). False switches it off (to measure the difference).
COLLECT_AFTER_ITEM = True
FULL_COLLECT_EVERY = 50

# The memory guard. Fusion's memory can grow over a long run (by gigabytes over thousands of
# designs, in Fusion itself), and a Fusion that runs out of memory dies with whatever it had open.
# Between items the run then stops cleanly, as if cancelled, with stopped_code 'memory', and the
# next run (after restarting Fusion) carries on where it stopped. A guard that stops a healthy run
# is worse than none, so it stops only when restarting Fusion would actually help, and only when
# that is so for a while, never on one sample:
#   'fusion': Fusion itself holds more than MEMORY_FUSION_SHARE of the computer's physical memory; or
#   'growth': Fusion has grown by more than MEMORY_GROWTH since the run started AND the computer is
#             under real memory pressure (system_pressure: the operating system's own signal, not
#             how little memory is free, which other programs decide as much as Fusion);
# and never while Fusion holds less than MEMORY_FUSION_MIN, never before MEMORY_MIN_ITEMS items of
# the run, and only once the rule has held on MEMORY_SUSTAIN_CHECKS checks in a row (one check
# after each item Fusion worked on) spanning at least MEMORY_SUSTAIN_SECONDS.
# When the computer is critically short of memory for as long (critical_pressure) but Fusion is not
# the cause, the run goes on and the report says so (LOW_MEMORY_NOTE): closing other programs
# helps, restarting Fusion would not.
# A rule whose figures cannot be read is not applied; with no figures at all the guard is off. It
# can be switched off (MEMORY_GUARD_ENV, or "memory_guard": false in settings.json or the job).
#
# The numbers, against what real runs measured:
#   8 GB Mac: Fusion 0.7-1.1 GB, "available" (free + inactive pages) 1.4-2.1 GB, dipping for ~2 s
#     while a design opens. Normal, nothing wrong: must not stop (Fusion is under MEMORY_FUSION_MIN
#     and far under 70% of 8 GB; a dip is one sample, not a sustained condition). The earlier rule
#     (available under 1.5 GB) stopped every launch after 1-11 designs.
#   32 GB Windows, Fusion 1.4 GB, other programs holding most of the memory (available 1.35 GB):
#     must not stop, since restarting Fusion would not help (Fusion under MEMORY_FUSION_MIN); the
#     report gets LOW_MEMORY_NOTE when the pressure lasts.
#   32 GB Windows, Fusion grown from 1.5 GB to 10.9 GB with 1.5 GB available: legitimate; it may
#     stop once the growth rule holds over MEMORY_SUSTAIN_CHECKS checks and MEMORY_SUSTAIN_SECONDS
#     with Windows short of commit (10.9 GB is 34% of 32 GB, so the 'fusion' rule alone does not).
MEMORY_FUSION_SHARE = 0.70          # Fusion alone over 70% of physical memory: it is the problem
MEMORY_GROWTH = 4 << 30             # 4 GB grown in one run (a slow leak's 1.4 MB/design is ~3000 designs)
MEMORY_FUSION_MIN = 2 << 30         # under 2 GB, restarting Fusion cannot give back enough to matter
MEMORY_MIN_ITEMS = 25               # items Fusion worked on in this run before the guard may stop it
MEMORY_SUSTAIN_CHECKS = 5           # the rule must hold on this many checks in a row...
MEMORY_SUSTAIN_SECONDS = 30         # ...spanning at least this long (a design's dip lasts ~2 s)
# Under pressure: on macOS the kernel's pressure level at warning (2) or critical (4). On Windows,
# what is left of the commit limit (physical memory plus page file: when it runs out, programs
# fail to get memory and can die) is under MEMORY_COMMIT_FLOOR or MEMORY_COMMIT_SHARE of the limit,
# while available physical memory is under MEMORY_PHYSICAL_FLOOR or MEMORY_PHYSICAL_SHARE of it.
MEMORY_PRESSURE_LEVEL = 2
# The low-memory note ("closing other programs may speed it up") needs more: macOS's critical
# level (4), not warning (2), which an 8 GB Mac with Fusion open reports about 96% of the time;
# on Windows the same commit and physical rule. Like the stop rules, only when it lasts
# (MEMORY_SUSTAIN_CHECKS checks over MEMORY_SUSTAIN_SECONDS).
MEMORY_CRITICAL_LEVEL = 4
MEMORY_COMMIT_FLOOR = 1 << 30       # 1 GB
MEMORY_COMMIT_SHARE = 0.05
MEMORY_PHYSICAL_FLOOR = 1 << 30     # 1 GB
MEMORY_PHYSICAL_SHARE = 0.10
MEMORY_REASON = 'Fusion is using a lot of memory'
LOW_MEMORY_NOTE = 'This computer was low on memory during the export; closing other programs may speed it up.'
# The kill switch: "off" (or 0, false, no) in Fusion's environment switches the guard off. The
# command-line launcher passes it on in the job, so it works from the launcher's environment too.
MEMORY_GUARD_ENV = 'FUSION_BULK_EXPORT_MEMORY_GUARD'
# For testing on a real Fusion only, off by default and not offered to users: with this set in
# Fusion's environment, an assembly of linked designs also gets a second archive with its links
# removed in memory ("<name> vN (with linked parts, experimental).f3d", FusionAdapter.linked_copy).
# Such copies crashed Fusion when opened in tests on real Fusion, so the recipe that removes the
# links is still being tested: the value picks it (LINK_RECIPES). The command-line launcher passes
# it on in the job ("experiment_linked_copies"), as it does MEMORY_GUARD_ENV.
LINKED_COPIES_ENV = 'FUSION_BULK_EXPORT_EXPERIMENT_LINKED_COPIES'
# innermost: deepest linked occurrence first, adsk.doEvents after each breakLink.
# compute: outermost first (doEvents after each pass), then design.computeAll() and SETTLE_EVENTS
#   doEvents before the copy is exported.
# both (the value 1): innermost first with doEvents after each breakLink, then computeAll and
#   SETTLE_EVENTS doEvents.
RECIPE_INNERMOST, RECIPE_COMPUTE, RECIPE_BOTH = 'innermost', 'compute', 'both'
LINK_RECIPES = {'1': RECIPE_BOTH, RECIPE_BOTH: RECIPE_BOTH, RECIPE_INNERMOST: RECIPE_INNERMOST,
                RECIPE_COMPUTE: RECIPE_COMPUTE}
# For testing on a real Fusion only: with this set (a number of MB) in Fusion's environment, the
# guard also stops at once (no minimum, no waiting) once Fusion holds more than that.
MEMORY_TEST_ENV = 'FUSION_BULK_EXPORT_TEST_MEMORY_MB'


def system_pressure(sample):
    """Whether the computer is under real memory pressure now (True or False), from the operating
    system's own figures; None when this sample does not say (no rule applies then)."""
    sample = sample or {}
    level = sample.get('pressure_level')
    if level is not None:
        return level >= MEMORY_PRESSURE_LEVEL
    limit, left = sample.get('commit_total'), sample.get('commit_available')
    total, available = sample.get('total'), sample.get('available')
    if limit is not None and left is not None and available is not None:
        short_of_commit = left < max(MEMORY_COMMIT_FLOOR, MEMORY_COMMIT_SHARE * limit)
        physical_floor = MEMORY_PHYSICAL_FLOOR if total is None else max(MEMORY_PHYSICAL_FLOOR,
                                                                          MEMORY_PHYSICAL_SHARE * total)
        return short_of_commit and available < physical_floor
    return None


def critical_pressure(sample):
    """Whether the computer is critically short of memory now (for the low-memory note): macOS's
    pressure level at critical; on Windows, as system_pressure. None when the sample does not say."""
    level = (sample or {}).get('pressure_level')
    if level is not None:
        return level >= MEMORY_CRITICAL_LEVEL
    return system_pressure(sample)


def memory_condition(start, last):
    """The guard rule that holds for this one sample ('fusion' or 'growth'), or None. start and
    last: memory samples (memory.sample()'s keys, any value None when unknown). Whether to stop
    also needs the rule to last: MemoryGuard."""
    if not last:
        return None
    rss, total = last.get('rss'), last.get('total')
    if rss is None or rss < MEMORY_FUSION_MIN:
        return None
    if total is not None and rss > MEMORY_FUSION_SHARE * total:
        return 'fusion'
    begun = (start or {}).get('rss')
    if begun is not None and rss - begun > MEMORY_GROWTH and system_pressure(last):
        return 'growth'
    return None


class Streak:
    """Checks in a row on which something held: how many, and since when."""
    def __init__(self):
        self.what, self.count, self.since = None, 0, None

    def add(self, what, now):
        """Count this check (what: the thing that held, None for nothing)."""
        if what is None or what != self.what:
            self.what, self.count, self.since = what, 0, now
        if what is not None:
            self.count += 1

    def lasting(self, now):
        return (self.what is not None and self.count >= MEMORY_SUSTAIN_CHECKS
                and now - self.since >= MEMORY_SUSTAIN_SECONDS)


class MemoryGuard:
    """Decides, check by check (one after each item Fusion worked on), whether the run stops for
    memory; also whether the computer was under lasting pressure Fusion did not cause (low_memory).
    clock: seconds (time.monotonic; tests pass a fake)."""
    def __init__(self, enabled=True, clock=time.monotonic, test_limit=None):
        self.enabled, self.clock, self.test_limit = enabled, clock, test_limit
        self.items = 0
        self.rule = Streak()        # a stop rule holding check after check
        self.pressure = Streak()    # critical system pressure check after check (the note)
        self.low_memory = False

    def check(self, start, sample):
        """The rule that stops the run now ('fusion', 'growth' or 'test'), or None."""
        now = self.clock()
        self.items += 1
        sample = sample or {}
        rss = sample.get('rss')
        if self.enabled and self.test_limit is not None and rss is not None and rss > self.test_limit:
            return 'test'
        rule = memory_condition(start, sample)
        self.rule.add(None if rule is None else 'stop', now)  # either rule, one after the other, counts
        self.pressure.add(True if critical_pressure(sample) else None, now)
        if rule is None and self.pressure.lasting(now):
            self.low_memory = True  # pressure that restarting Fusion would not cure
        if not self.enabled or self.items < MEMORY_MIN_ITEMS:
            return None
        return rule if self.rule.lasting(now) else None


def memory_guard_enabled(environ=None, settings=None):
    """False when the guard is switched off: MEMORY_GUARD_ENV is off/0/false/no, or settings.json
    has "memory_guard": false."""
    value = (os.environ if environ is None else environ).get(MEMORY_GUARD_ENV)
    if value is not None and value.strip().lower() in ('off', '0', 'false', 'no'):
        return False
    if settings is None:
        from . import settings as stored
        settings = stored.load()
    return settings.get('memory_guard') is not False


def link_recipe(value):
    """The recipe a LINKED_COPIES_ENV value (or a job's "experiment_linked_copies") asks for, or
    None (off): 1 is RECIPE_BOTH; innermost and compute name theirs; anything else is off. A bool
    from a caller is accepted too (True: RECIPE_BOTH)."""
    if value is True:
        return RECIPE_BOTH
    if not isinstance(value, str):
        return None
    return LINK_RECIPES.get(value.strip().lower())


def linked_copies_experiment(environ=None):
    """The recipe LINKED_COPIES_ENV in this process's environment asks for, or None (off, the
    default)."""
    return link_recipe((os.environ if environ is None else environ).get(LINKED_COPIES_ENV))


def memory_test_limit(environ=None):
    """MEMORY_TEST_ENV in bytes, or None (unset or not a positive number)."""
    value = (os.environ if environ is None else environ).get(MEMORY_TEST_ENV)
    try:
        megabytes = float(value)
    except (TypeError, ValueError):
        return None
    return int(megabytes * (1 << 20)) if megabytes > 0 else None


# Run counts, by what happened to each item this run (linked_copies: designs exported this run that
# also got their experimental copy with linked parts, LINKED_COPIES_ENV; also counted in exported).
COUNT_KEYS = ('exported', 'up_to_date', 'files', 'files_up_to_date', 'skipped', 'unsupported', 'failed',
              'linked_copies')
COUNT_OF_STATUS = {'done': 'exported', 'verified': 'up_to_date', 'skipped': 'skipped', 'failed': 'failed',
                   'unsupported': 'unsupported'}
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
    files: other files (PDFs, uploads) saved this run; files_up_to_date: other files
    saved before and checked; skipped: not saved, with a reason (other files when only designs are
    wanted, a design without a design product, a download Fusion refused); unsupported: a file
    type Fusion's API cannot export at all (electronics, drawings: see UNSUPPORTED), neither a
    failure nor the user's choice; failed: will be tried again on the next run; linked_copies:
    designs exported this run that also got their experimental copy with linked parts (also in
    exported; always 0 unless LINKED_COPIES_ENV is on)."""
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
        return '%s, %s' % (plural(whole, 'whole project'), plural(folders, 'folder'))

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


DRAWING = 'f2d'
# Drawings are never opened or exported: on real Fusion, exporting a drawing's PDF through the API
# crashed Fusion itself (inside its sheet export, after minutes of opening the drawing). They are
# recorded as unsupported with this reason.
DRAWING_REASON = "Drawing not exported: Fusion's drawing export crashes when an add-in runs it."


def is_drawing(file):
    return str(getattr(file, 'fileExtension', '') or '').lower().strip('.') == DRAWING


# Fusion's own types other than designs: DataFile.download refuses them ("file type not available
# for download", seen on real Fusion for .f2d, .fsch, .fbrd and .flbr) and the API has no export
# for them, so they are recorded as unsupported (status "unsupported") without asking Fusion.
UNSUPPORTED = {'f2d', 'f2t', 'fsch', 'fbrd', 'flbr', 'fprj', 'cam360'}
# Fusion's electronics types; the other types in UNSUPPORTED get the general reason.
ELECTRONICS = {'fsch', 'fbrd', 'flbr', 'fprj'}
ELECTRONICS_REASON = 'Electronics file (.%s) not exported: Fusion offers no export for it to add-ins.'
NATIVE_REASON = 'File (.%s) not exported: Fusion offers no export for this file type to add-ins.'
# What versions up to 0.3.1 wrote for these (their manifests are still read).
LEGACY_NATIVE_REASON = 'Fusion cannot export this file type through its API'


def native_reason(extension):
    """Why a file of one of Fusion's own types (UNSUPPORTED) is not exported, in plain words."""
    extension = str(extension or '').lower().strip('.')
    if extension in ('f2d', 'f2t'):
        return DRAWING_REASON
    return (ELECTRONICS_REASON if extension in ELECTRONICS else NATIVE_REASON) % extension


def extension_of(file):
    return str(getattr(file, 'fileExtension', '') or '').lower().strip('.')


def unsupported_reason(file):
    """Why Fusion's API cannot export this file, or None when it can (or may)."""
    extension = extension_of(file)
    if extension == DRAWING:
        return DRAWING_REASON
    if extension in UNSUPPORTED:
        return native_reason(extension)
    return None


EXTENSION = re.compile(r'(?=.*[A-Za-z])[A-Za-z0-9]{1,8}')


def file_name_parts(file):
    """(name, extension) of another file on disk. Fusion's DataFile.name of an uploaded file
    already ends in its real extension (and fileExtension can disagree with it), so the name is
    trusted: "Manual.PDF" stays "Manual.PDF", "notes.scr" stays so even when Fusion calls it htm.
    A name without an extension gets fileExtension."""
    name = str(file.name)
    declared = str(getattr(file, 'fileExtension', '') or '').strip().strip('.')
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


# What each recorded file of an entry is: files[i]['role']. Entries written before roles existed
# have none; checks.file_role() tells them apart by extension.
ROLE_ARCHIVE, ROLE_STEP, ROLE_LINKED, ROLE_THUMBNAIL, ROLE_FILE = (
    'archive', 'step', 'archive_with_linked_parts', 'thumbnail', 'file')


def design_role(path):
    """The role of a file a design export wrote, from its path."""
    text = str(path).lower()
    if text.endswith(LINKED_SUFFIX.lower()):
        return ROLE_LINKED
    if text.endswith('.png'):
        return ROLE_THUMBNAIL
    return ROLE_STEP if checks.is_step(text) else ROLE_ARCHIVE


def cloud_state(file):
    """(versionNumber, latestVersionNumber, dateModified) of a cloud file, read from the DataFile
    in hand (properties, nothing is listed or opened); None when they cannot be read."""
    try:
        return (file.versionNumber, getattr(file, 'latestVersionNumber', None), file.dateModified)
    except Exception:
        return None


ACCOUNT_CHANGED = ("A design's version or date in your Fusion account changed while it was exported "
                   "with its linked parts; the export stopped to be safe.")


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


# A design entry's "step": absent when its STEP was written; STEP_OFF when the run did not ask for
# one; STEP_FAILED when Fusion would not write it (the .f3d stands; step_error says why).
STEP_OFF, STEP_FAILED = 'off', 'failed'
STEP_NOT_REQUESTED = 'STEP not requested'
STEP_FAILED_WARNING = 'Saved, but Fusion could not write its STEP file'
STEP_OFF_NOTE = 'STEP files were not requested for this export.'


# What happens to a design's older versions once its newest is in the folder, complete and checked
# (the dialog's "Older versions of a design", the job's "older_versions"; Exporter.tidy_older_versions):
# OLDER_MOVE (the default) moves the files this export wrote for them into the "Older versions"
# folder (names.OLDER_FOLDER) at the top of the export folder, under the same relative path; their
# entries stay in the manifest with status MOVED, the date (moved) and where each file went
# (moved_files). OLDER_DELETE deletes those files; the entries get status REMOVED, the date
# (removed) and what was removed (removed_files). OLDER_KEEP leaves them beside the newest.
OLDER_MOVE, OLDER_KEEP, OLDER_DELETE = 'move', 'keep', 'delete'
OLDER_VERSIONS = (OLDER_MOVE, OLDER_KEEP, OLDER_DELETE)
REMOVED = 'removed'
MOVED = 'moved'
KEPT_CHANGED = 'kept: changed since it was exported'
KEPT_SHARED = 'kept: another item records the same file'
KEPT_OUTSIDE = 'kept: not inside the export folder'
KEPT_NOT_FILE = 'kept: not a plain file'


def older_versions_choice(value):
    """OLDER_MOVE, OLDER_KEEP or OLDER_DELETE; anything else (a setting from a test build, a
    typo) is OLDER_MOVE, the default."""
    return value if isinstance(value, str) and value in OLDER_VERSIONS else OLDER_MOVE


def numbered(path, number):
    """path with " (number)" before its extension: "Part v3 (2).step"."""
    stem, extension = os.path.splitext(path.name)
    return path.with_name('%s (%d)%s' % (stem, number, extension))


def recorded_files(entry):
    """Every file a manifest entry records: its files and its thumbnail."""
    infos = list(entry.get('files') or [])
    if (entry.get('thumbnail') or {}).get('path'):
        infos.append(entry['thumbnail'])
    return infos


def has_step(entry):
    """Whether a design entry is meant to record a STEP file (see STEP_OFF, STEP_FAILED)."""
    return entry.get('step') not in (STEP_OFF, STEP_FAILED)


def complete(entry):
    """Whether a finished entry records the files its kind needs: another file one file; a
    design its archive and its STEP (none when it was not requested or Fusion would not write
    it), plus at most a copy with linked parts. By role, and for entries written before roles
    existed by extension (checks.file_role), in any order."""
    files = entry.get('files') or []
    if entry.get('kind') == 'file':
        return len(files) == 1
    roles = [checks.file_role(info) for info in files]
    steps = 1 if has_step(entry) else 0
    return (roles.count(ROLE_ARCHIVE) == 1 and roles.count(ROLE_STEP) == steps
            and roles.count(ROLE_LINKED) <= 1 and len(roles) == 1 + steps + roles.count(ROLE_LINKED))


def link_paths(manifest):
    """The link map: on every design's references, path is the relative path of that linked
    design's exported archive in this folder (its newest exported version), or None when it is not
    exported here. Run at the end of every run, since a linked design is often exported after the
    assembly that uses it."""
    archives = {}
    for entry in manifest.get('entries') or []:
        if entry.get('kind', 'design') != 'design' or entry.get('status') != 'done':
            continue
        archive = next((info.get('path') for info in entry.get('files') or []
                        if checks.file_role(info) == ROLE_ARCHIVE), None)
        known = archives.get(str(entry.get('id')))
        if archive and (known is None or (entry.get('version') or 0) >= known[0]):
            archives[str(entry.get('id'))] = (entry.get('version') or 0, archive)
    for entry in manifest.get('entries') or []:
        for reference in entry.get('references') or []:
            if isinstance(reference, dict):
                found = archives.get(str(reference.get('id')))
                reference['path'] = found[1] if found else None


def free_bytes(path):
    """Free space on the disk holding path (or its nearest existing parent), or None."""
    path = Path(path)
    for candidate in [path] + list(path.parents):
        # The long-path form first; a network share (\\\\server\\share) can refuse it, so then the
        # plain one.
        for form in dict.fromkeys((long_path(candidate), str(candidate))):
            try:
                return shutil.disk_usage(form).free
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


LINKS_UNREADABLE = 'Links to other designs could not be read; the design itself was exported.'


def reference_facts(file, problems=None):
    """has_references and references_out_of_date of a cloud file: DataFile properties
    (hasChildReferences, hasOutofDateChildReferences), read where they are, without listing
    anything. A property this Fusion lacks, or one that cannot be read, is None (unknown).
    Reading them can raise on real Fusion ("2 : InternalValidationError : status.isOk() &&
    relationships"); the error text is added to problems (a list) when given, never raised."""
    facts = {}
    for key, member in (('has_references', 'hasChildReferences'),
                        ('references_out_of_date', 'hasOutofDateChildReferences')):
        try:
            value = getattr(file, member, None)
            facts[key] = None if value is None else bool(value)
        except Exception as error:
            facts[key] = None
            if problems is not None:
                problems.append(str(error) or type(error).__name__)
    return facts


def link_reason(count):
    """The plain failure reason of a design with links that could not be opened or exported."""
    if count:
        return 'A linked design is missing or cannot be opened (%d link%s).' % (count, '' if count == 1 else 's')
    return 'A linked design is missing or cannot be opened.'


class Trouble(Exception):
    """A problem that stops the whole run (not just one design). error is the text of the error
    that caused it (or None): never the exception object, whose traceback would keep the frames
    it passed through alive, with the Fusion documents and designs in their variables."""
    def __init__(self, code, reason, error=None):
        super().__init__(reason)
        self.code, self.reason = code, reason
        self.error = None if error is None else (str(error) or type(error).__name__)


def forget_frames(error):
    """Drop the variables of the finished frames an exception passed through (the Fusion document,
    design and export manager of a failed export among them), so that nothing that still holds the
    exception for a moment keeps them alive. The frame running now is left as it is."""
    try:
        traceback.clear_frames(error.__traceback__)
    except Exception:
        pass


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
        # Absolute, never resolved: a mapped drive (X:) keeps its letter in the manifest, the
        # report's paths and the summary (resolve() turned it into its \\\\server\\share path).
        self.root = Path(absolute_path(output))
        problem = output_root_problem(self.root, sys.platform)
        if problem:
            raise ValueError(problem)
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
        self.upgraded = False  # this run opened a folder written by version 0.2 (schema 1)
        if os.path.exists(long_path(self.manifest_path)):
            with open(long_path(self.manifest_path), encoding='utf-8') as stream:
                self.manifest = json.loads(stream.read())
            if self.manifest.get('schema') not in (1, SCHEMA):
                raise ValueError('Unsupported manifest schema')
            # Schema 1 entries keep their (shortened, hashed) files; new exports use real names,
            # so such a folder holds both layouts (the report says so: older_layout).
            if self.manifest.get('schema') == 1:
                self.manifest['older_layout'] = True
                self.upgraded = True
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
        # Also write each design's STEP (the dialog's box, the job's "step"); set by run().
        self.step = True
        # Testing only, off (None) by default: the recipe (LINK_RECIPES) for the experimental
        # second archive of an assembly with its linked parts included (FusionAdapter.linked_copy).
        # Set by run() from LINKED_COPIES_ENV or the job's "experiment_linked_copies".
        self.linked_copies = None
        # What happens to each design's older versions once its newest is in the folder
        # (OLDER_VERSIONS; the dialog's choice, the job's "older_versions"); set by run().
        self.older_versions = OLDER_MOVE
        # Set when the account guard (export_checked) saw a design change in the account.
        self.alarm = None
        # Fusion's CAM API (adsk.cam) for the tool libraries; None imports it when they are
        # exported (toollibs.load_cam). Tests set a fake.
        self.cam = None
        # Fusion's memory, sampled between items (memory.sample inside Fusion; tests set a fake).
        self.memory_sampler = memory.sample if inside_fusion() else None
        # False switches the memory guard off for this exporter (a job's "memory_guard": false);
        # MEMORY_GUARD_ENV and settings.json can switch it off too (memory_guard_enabled).
        self.memory_guard = True
        self.memory_clock = time.monotonic
        self.run_info = {'started': now(), 'finished': None, 'cancelled': False}
        if self.upgraded:
            self.run_info['upgraded_schema'] = 1
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
        if entry.get('status') != 'done' or not complete(entry):
            return False
        if self.step and entry.get('kind') != 'file' and not has_step(entry):
            return False  # its STEP is wanted now (not requested then, or Fusion would not write it)
        copy = entry.get('linked_copy') or {}
        if self.linked_copies and copy.get('retry'):
            return False  # the experimental copy was left out for now (the design was open)
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
            if not too_long(str(stem) + self.longest_suffix(), self.platform) and self.make_directory(directory):
                return stem, facts
        directory = self.short_directory(hub, project, folders)
        stem = directory / (sanitize(file.name, 12) + '~' + digest(str(file.id))[:24] + version)
        if too_long(str(stem) + self.longest_suffix(), self.platform):
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
            on_trouble=None, other_files=True, linked_copies=None, step=True, older_versions=OLDER_MOVE,
            tool_libraries=False, designs=True):
        """Export as designs are found: each folder is listed, its designs exported, then the next.

        tool_libraries: first, before any design, save the user's CAM tool libraries (toollibs.py:
        one .json file each in "Tool libraries", recorded in manifest["tool_libraries"], this run's
        tally in run_info["tool_libraries"]). Off unless asked for: the dialog and the job ask for
        it by default. A Fusion whose CAM API is missing or fails gives a warning there, never a
        failed run. designs: False exports no designs or other files at all (only the tool
        libraries: the command line's --tool-libraries-only).

        limit: stop once this many designs were exported in this run (designs already up to
        date, failures and other files do not count). Other files are saved in every folder
        visited until then, including the rest of the folder in which the limit was reached;
        its remaining designs, and every later folder, are left for the next run.

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

        other_files: also save the files that are not designs (PDFs, uploads) as Fusion hands
        them out; when false they are skipped as "Not a Fusion design".

        step: also write each design's STEP (on by default). Off, a design gets its .f3d only and
        its entry records step: "off"; a later run with it on re-exports those designs to add the
        STEP. A design exported with its STEP stays up to date in a run with it off (nothing is
        deleted).

        older_versions: what happens to the files this export recorded for a design's older
        versions once its newest is in the folder (exported now or already up to date): OLDER_MOVE
        (the default) moves them into the "Older versions" folder, OLDER_DELETE deletes them,
        OLDER_KEEP leaves them where they are. See tidy_older_versions for exactly which files.
        Only OLDER_DELETE ever deletes a file the export saved.

        linked_copies: testing only, off by default. None reads LINKED_COPIES_ENV from this
        process's environment; a recipe name or a job's value (link_recipe) switches it on, False
        off. When on, a design with linked components also gets an experimental second archive
        with its links removed in memory (FusionAdapter.linked_copy). A design whose account
        version or date changes meanwhile (which must never happen) stops the run at once,
        without asking on_trouble: stopped_code 'account_changed'.

        Drawings (.f2d) are never opened: they are recorded as unsupported (DRAWING_REASON).

        The output folder is locked (export.lock) for the run: FolderBusy is raised when another
        export is using it.
        """
        lock = FolderLock(self.root)
        lock.acquire()
        try:
            return self._run(data, cancelled, progress, limit, selection, on_trouble, other_files, linked_copies,
                             step, older_versions, tool_libraries, designs)
        finally:
            lock.release()

    def _run(self, data, cancelled, progress, limit, selection, on_trouble, other_files, linked_copies=None,
             step=True, older_versions=OLDER_MOVE, tool_libraries=False, designs=True):
        self.other_files = bool(other_files)
        self.step = bool(step)
        self.older_versions = older_versions_choice(older_versions)
        self.run_info['older_versions'] = self.older_versions
        if self.older_versions != OLDER_KEEP:
            self.run_info[MOVED if self.older_versions == OLDER_MOVE else REMOVED] = \
                dict(versions=0, files=0, kept=0, errors=0)
        self.linked_copies = linked_copies_experiment() if linked_copies is None else link_recipe(linked_copies)
        self.run_info.update(other_files=self.other_files, step=self.step)
        if not designs:
            self.run_info['designs'] = False
        if self.linked_copies:
            self.run_info['experiment_linked_copies'] = self.linked_copies
            self.log('experiment: copies with linked parts on, recipe ' + self.linked_copies)
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
        self.begin_documents()
        self.save()
        found = 0
        failing = []  # errors of the designs that failed one after another
        stopping = None
        tracker = None
        try:
            self.log('run started')
            if tool_libraries:
                stopping = self.export_tool_libraries(cancelled, progress)
                if stopping is not None:
                    self.run_info.update(stopped_reason=stopping.reason, stopped_code=stopping.code)
                    self.log('trouble: ' + stopping.reason + ' ' + (stopping.error or ''))
                elif cancelled():
                    self.run_info['cancelled'] = True
                self.save()  # the libraries are in manifest.json before the first design
            if not designs or stopping is not None or self.run_info['cancelled']:
                data = _View()
                data.dataHubs = Listing([])  # nothing to walk: no design or other file
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
            tracker = memory.Tracker(self.memory_sampler, self.run_info.setdefault('memory', {}), self.log)
            if tracker.begin() is None:
                self.run_info.pop('memory', None)  # nothing could be sampled: the guard is off
            guard = MemoryGuard(bool(self.memory_guard) and memory_guard_enabled(), self.memory_clock,
                                memory_test_limit())
            if tracker.active and not guard.enabled:
                self.run_info['memory_guard'] = 'off'
                self.log('memory guard: off')
            finishing = None  # the folder in which the limit was reached: only its other files go on
            with keep_awake():
                for hub, project, folders, file in walk(data, unreadable, None if selection.everything else selection,
                                                        visiting):
                    if cancelled():
                        self.run_info['cancelled'] = True
                        break
                    if not selection.keeps(hub, project, folders):
                        continue
                    if finishing is not None:
                        if finishing != (str(hub.id), str(project.id), list(folders)):
                            break  # the limit's folder is done
                        if self.item_kind(file) == 'design':
                            continue  # left for the next run
                    found += 1
                    place(hub, project, folders)
                    state.update(design=found, name=file.name, folder_item=state['folder_item'] + 1,
                                 kind=self.item_kind(file))
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
                        # Its traceback holds export_one's frame (the item's DataFile): not kept.
                        status, problem = 'failed', error.with_traceback(None)
                    except OSError as error:  # writing the manifest or log
                        problem = self.trouble(error)
                        if problem is None:
                            raise
                        status = 'failed'
                    if status != 'verified' and str(file.fileExtension).lower() == 'f3d':
                        timing['seconds'] += self.clock() - began
                        timing['designs'] += 1
                    sample = None
                    if status != 'verified':
                        self.collect()  # Fusion worked on this item: release what it left behind
                        sample = tracker.update(timing['designs'])
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
                        # The account changed: never carried on, whatever anyone would answer.
                        answer = (on_trouble(problem.reason, last) if on_trouble and problem.code != 'account_changed'
                                  else 'stop')
                        if answer != 'retry':
                            stopping = problem
                            self.run_info.update(stopped_reason=problem.reason, stopped_code=problem.code)
                            break
                        failing = []
                    rule = guard.check(tracker.start, sample) if sample is not None else None
                    if guard.low_memory and not self.run_info.get('low_memory'):
                        self.run_info['low_memory'] = True
                        self.log('low memory: the computer is under memory pressure, Fusion is not the cause')
                    if rule is not None:
                        # Between items, like a Cancel: everything so far is saved; a run after
                        # restarting Fusion carries on from here.
                        stopping = Trouble('memory', MEMORY_REASON)
                        self.run_info.update(stopped_reason=MEMORY_REASON, stopped_code='memory', memory_rule=rule)
                        self.log('stopped: memory (%s rule, %d checks in a row), Fusion %s MB, available %s MB' % (
                            rule, guard.rule.count, '?' if sample.get('rss') is None else sample['rss'] >> 20,
                            '?' if sample.get('available') is None else sample['available'] >> 20))
                        break
                    if limit is not None and counts['exported'] >= limit and finishing is None:
                        # A small run on a large account need not list everything; the other files
                        # of this folder are still saved (they do not count towards the limit).
                        finishing = (str(hub.id), str(project.id), list(folders))
                        self.run_info['limit_reached'] = True
        except Exception as error:
            self.run_info['error'] = str(error)
            raise
        finally:
            if tracker is not None:
                tracker.finish()
            self.finish_documents()
            self.staging.clean()
            link_paths(self.manifest)  # every link to its exported file, now that the run's exports are known
            self.run_info.update(finished=now(), counts=counts, found=found)
            try:
                self.save()
            except OSError:
                if stopping is None:
                    raise  # with a stop reason, the folder is known to be unusable
            self.log('run finished ' + json.dumps(counts))
        return counts

    def export_tool_libraries(self, cancelled, progress):
        """The tool library part of the run (toollibs.ToolLibraryExport): the Trouble that stops the
        run (the output folder cannot be written), else None. Anything else that goes wrong is a
        warning in run_info["tool_libraries"]: the designs are exported all the same."""
        from . import toollibs

        def step(number, found):
            progress(dict(phase='tool_libraries', project=0, projects=0, project_name=None, path=list(found.folder),
                          design=number, name=found.name, kind=toollibs.KIND, folder_files=None, folder_item=0,
                          counts=new_counts(), elapsed=0.0, seconds_per_design=None, location=found.location))
        try:
            return toollibs.ToolLibraryExport(self).run(self.cam, cancelled, step)
        except Exception as error:
            tally = self.run_info.get('tool_libraries')
            if not isinstance(tally, dict):
                tally = self.run_info['tool_libraries'] = toollibs.new_tally()
            tally['warning'] = 'Fusion raised an error while they were read: %s' % (str(error) or type(error).__name__)
            self.log('tool libraries not exported: ' + (str(error) or type(error).__name__))
            if self.error_callback:
                self.error_callback('tool libraries', error)
            return None

    def begin_documents(self):
        """Ask the adapter (when it can) to remember the documents open in Fusion as the run
        begins; run_info['documents_at_start'] is how many."""
        begin = getattr(self.adapter, 'begin_run', None)
        if begin is None:
            return
        try:
            count = begin()
        except Exception as error:
            self.log('could not list the open documents: ' + (str(error) or type(error).__name__))
            return
        self.run_info['documents_at_start'] = count
        self.log('documents open at start: %s' % count)

    def finish_documents(self):
        """At the end of the run: the adapter closes the documents the run left open (never one
        open when it began; see FusionAdapter.finish_run). run_info records documents_at_end and
        closed_leftovers (documents closed after their own item had finished). Never raises."""
        finish = getattr(self.adapter, 'finish_run', None)
        if finish is None or 'documents_at_start' not in self.run_info:
            return
        try:
            count, closed = finish()
        except Exception as error:
            self.log('could not close the documents left open: ' + (str(error) or type(error).__name__))
            return
        self.run_info.update(documents_at_end=count, closed_leftovers=closed)
        self.log('documents open at end: %s (at start %s), left open by the run and closed: %d'
                 % (count, self.run_info['documents_at_start'], closed))

    def collect(self):
        """Run Python's cycle collector once (see COLLECT_AFTER_ITEM), and add its cost to
        run_info['gc']: collections (full: how many were full), seconds, and objects it found unreachable (objects near 0
        run after run means the add-in leaves no cycles behind)."""
        if not COLLECT_AFTER_ITEM:
            return
        stats = self.run_info.setdefault('gc', dict(collections=0, full=0, seconds=0.0, objects=0))
        full = (stats['collections'] + 1) % FULL_COLLECT_EVERY == 0
        began = time.perf_counter()
        found = gc.collect() if full else gc.collect(1)
        stats['collections'] += 1
        stats['full'] += 1 if full else 0
        stats['seconds'] = round(stats['seconds'] + time.perf_counter() - began, 3)
        stats['objects'] += found

    def item_kind(self, file):
        """What the progress text calls an item: 'design' or 'file'."""
        return 'design' if str(file.fileExtension).lower() == 'f3d' else 'file'

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
        began = self.clock()
        key = self.item_key(hub, project, file)
        kind = 'design' if str(file.fileExtension).lower() == 'f3d' else 'file'
        entry = entries.get(key)
        if entry and self.valid(entry):
            counts[count_key(entry, 'verified')] += 1
            self.log('verified ' + key)
            if self.refreshed:
                self.record(entry)  # its files' times were recorded now; else nothing to save
            self.tidy_older_versions(entry)
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
        # Written before Fusion is asked (opening can block it for minutes): the log then shows
        # which item is slow.
        self.log('exporting ' + key)
        if kind == 'file' and unsupported_reason(file) is not None:
            # Never handed to Fusion: neither a failure nor something the user left out.
            entry.update(status='unsupported', error=unsupported_reason(file))
        elif kind == 'file':
            if not self.other_files:
                entry['error'] = 'Not a Fusion design: ' + file.fileExtension
            elif hasattr(self.adapter, 'download'):
                trouble = self.save_file(hub, project, folders, file, entry, entries, owned)
            else:
                entry['error'] = 'Not a Fusion design: ' + file.fileExtension
        else:
            trouble = self.export_design(hub, project, folders, file, entry, key, owned)
        took = self.clock() - began
        if took > SLOW_SECONDS:
            entry['slow_seconds'] = int(round(took))
            self.run_info['slow'] = self.run_info.get('slow', 0) + 1
            self.log('slow (%d s) %s' % (entry['slow_seconds'], key))
        counts[count_key(entry, entry['status'])] += 1
        if entry['status'] == 'done' and (entry.get('linked_copy') or {}).get('status') == 'saved':
            counts['linked_copies'] = counts.get('linked_copies', 0) + 1
        self.log(entry['status'] + ' ' + key)
        if trouble is not None:
            raise trouble  # the run's final save keeps this entry if the disk still allows
        self.record(entry)  # recorded before anything older is moved or removed
        self.tidy_older_versions(entry)
        return entry['status']

    def tidy_older_versions(self, newest):
        """Unless older_versions is OLDER_KEEP, after newest (a design's entry for the version in
        the account) is done: move (OLDER_MOVE) or delete (OLDER_DELETE) what this export wrote
        for that design's older versions. Only:
        - when newest is a finished design entry, complete (complete()) and without a STEP failure
          (a STEP Fusion would not write leaves the older versions as they are);
        - files recorded in the manifest (files and thumbnail) of entries of the same design (cloud
          id) with a lower version number: never a file the manifest does not list, never another
          item's, never one another entry also records, never a path outside the export folder;
        - when every such file of that older entry still matches its record (size and SHA-256;
          size and modified time when no SHA-256 was recorded); otherwise none of that entry's
          files are touched and it records kept (KEPT_CHANGED or another reason).
        The older entry stays in the manifest: status MOVED with moved (the date) and moved_files
        (each file's path and moved_to, its new relative path), or status REMOVED with removed and
        removed_files. A file that cannot be moved or deleted stays recorded (move_error or
        remove_error says why) and is tried again on the next run; the run goes on. An entry left
        half done that way is finished the way it was begun, whatever this run's choice."""
        if self.older_versions == OLDER_KEEP:
            return
        if newest.get('kind', 'design') != 'design' or newest.get('status') != 'done':
            return
        if not complete(newest) or newest.get('step') == STEP_FAILED or not isinstance(newest.get('version'), int):
            return
        older = [entry for entry in self.manifest['entries']
                 if entry is not newest and entry.get('kind', 'design') == 'design'
                 and str(entry.get('id')) == str(newest.get('id'))
                 and isinstance(entry.get('version'), int) and entry['version'] < newest['version']
                 and (entry.get('status') == 'done'
                      or (entry.get('status') in (REMOVED, MOVED) and recorded_files(entry)))]
        if not older:
            return
        leaving = set(id(entry) for entry in older)
        protected = set(info.get('path') for entry in self.manifest['entries'] if id(entry) not in leaving
                        for info in recorded_files(entry))
        for entry in older:
            status = entry.get('status')
            if status == MOVED or (status == 'done' and self.older_versions == OLDER_MOVE):
                self.move_version(entry, protected)
            else:
                self.remove_version(entry, protected)

    def removable(self, info, protected):
        """(full path, None) when this recorded file may be removed; (None, None) when it is gone
        already; (None, reason) when it must be kept."""
        relative = info.get('path')
        if not relative or relative in protected:
            return None, KEPT_SHARED
        path = Path(os.path.normpath(str(self.root / relative)))
        if path == self.root or not path.is_relative_to(self.root):
            return None, KEPT_OUTSIDE
        try:
            stat = os.lstat(long_path(path))
        except FileNotFoundError:
            return None, None
        except OSError as error:
            return None, 'kept: could not be read (%s)' % (error.strerror or type(error).__name__)
        if not S_ISREG(stat.st_mode):
            return None, KEPT_NOT_FILE
        if info.get('bytes') is None or stat.st_size != info['bytes']:
            return None, KEPT_CHANGED
        if info.get('sha256'):
            try:
                same = checks.file_sha256(path) == info['sha256']
            except OSError as error:
                return None, 'kept: could not be read (%s)' % (error.strerror or type(error).__name__)
        else:
            same = info.get('mtime_ns') is not None and stat.st_mtime_ns == info['mtime_ns']
        return (path, None) if same else (None, KEPT_CHANGED)

    def older_plan(self, entry, protected, action):
        """([(info, full path or None, None), ...], the run's tally for action) for an older
        version whose recorded files may all be moved or deleted; (None, tally) when one must be
        kept (the entry then records why: kept)."""
        plan = [(info,) + self.removable(info, protected) for info in recorded_files(entry)]
        kept = next((reason for _, _, reason in plan if reason), None)
        tally = self.run_info.setdefault(action, dict(versions=0, files=0, kept=0, errors=0))
        if not kept:
            return plan, tally
        tally['kept'] += 1
        if entry.get('kept') != kept:
            entry['kept'] = kept
            self.log('older version %s %s' % (entry.get('key'), kept))
            self.record(entry)
        return None, tally

    def older_target(self, relative, info):
        """(relative path in the Older versions folder, whether the file is there already) for a
        recorded file: OLDER_FOLDER/<its relative path>, or with " (2)", " (3)"... before the
        extension when that name is taken by another file. A file there with the recorded size and
        SHA-256 is this one, moved by a run that stopped before saving its record."""
        base = self.root / OLDER_FOLDER / relative
        candidate, number = base, 1
        while True:
            try:
                stat = os.lstat(long_path(candidate))
            except FileNotFoundError:
                return candidate.relative_to(self.root).as_posix(), False
            if (S_ISREG(stat.st_mode) and info.get('sha256') and stat.st_size == info.get('bytes')
                    and checks.file_sha256(candidate) == info['sha256']):
                return candidate.relative_to(self.root).as_posix(), True
            number += 1
            candidate = numbered(base, number)

    def move_version(self, entry, protected):
        """Move one older version's recorded files into the Older versions folder (see
        tidy_older_versions). Moved within the export folder (one disk), never copied."""
        plan, tally = self.older_plan(entry, protected, MOVED)
        if plan is None:
            return
        gone, errors, moved = [], [], 0
        for info, path, _ in plan:
            relative = info.get('path')
            try:
                target, there = self.older_target(relative, info)
                if path is not None:
                    full = self.root / target
                    os.makedirs(long_path(full.parent), exist_ok=True)
                    os.replace(long_path(path), long_path(full))
                    moved += 1
                elif not there:
                    target = None  # gone already, and not in the Older versions folder
            except OSError as error:
                errors.append('%s: %s' % (relative, error.strerror or type(error).__name__))
                continue
            gone.append((info, target))
        if errors and not gone:
            # Nothing could be moved: the entry stays as it was, and the next run tries again.
            entry['move_error'] = errors[0]
            tally['errors'] += 1
            self.log('older version %s: could not move %s' % (entry.get('key'), '; '.join(errors)))
            self.record(entry)
            return
        taken = set(id(info) for info, _ in gone)
        entry.setdefault('moved_files', []).extend(
            dict(path=info.get('path'), moved_to=target, role=checks.file_role(info), bytes=info.get('bytes'),
                 sha256=info.get('sha256'))
            for info, target in gone)
        entry['files'] = [info for info in entry.get('files') or [] if id(info) not in taken]
        if id(entry.get('thumbnail')) in taken:
            entry.pop('thumbnail')
        entry.update(status=MOVED, moved=now())
        entry.pop('kept', None)
        entry.pop('long_path', None)
        if errors:
            entry['move_error'] = errors[0]
            tally['errors'] += 1
            self.log('older version %s: could not move %s' % (entry.get('key'), '; '.join(errors)))
        else:
            entry.pop('move_error', None)
        if moved:  # (files moved by a run that stopped before saving its record are not counted)
            tally['versions'] += 1
            tally['files'] += moved
        self.log('moved older version %s to %s (%s)' % (entry.get('key'), OLDER_FOLDER, plural(moved, 'file')))
        self.record(entry)

    def remove_version(self, entry, protected):
        """Delete one older version's recorded files (see tidy_older_versions)."""
        plan, tally = self.older_plan(entry, protected, REMOVED)
        if plan is None:
            return
        gone, errors, removed = [], [], 0
        for info, path, _ in plan:
            if path is not None:
                try:
                    os.unlink(long_path(path))
                    removed += 1
                except FileNotFoundError:
                    pass
                except OSError as error:
                    errors.append('%s: %s' % (info.get('path'), error.strerror or type(error).__name__))
                    continue
            gone.append(info)
        if errors and not gone:
            # Nothing could be removed: the entry stays as it was, and the next run tries again.
            entry['remove_error'] = errors[0]
            tally['errors'] += 1
            self.log('older version %s: could not remove %s' % (entry.get('key'), '; '.join(errors)))
            self.record(entry)
            return
        taken = set(id(info) for info in gone)
        entry.setdefault('removed_files', []).extend(
            dict(path=info.get('path'), role=checks.file_role(info), bytes=info.get('bytes')) for info in gone)
        entry['files'] = [info for info in entry.get('files') or [] if id(info) not in taken]
        if id(entry.get('thumbnail')) in taken:
            entry.pop('thumbnail')
        entry.update(status=REMOVED, removed=now())
        entry.pop('kept', None)
        entry.pop('long_path', None)
        if errors:
            entry['remove_error'] = errors[0]
            tally['errors'] += 1
            self.log('older version %s: could not remove %s' % (entry.get('key'), '; '.join(errors)))
        else:
            entry.pop('remove_error', None)
        if removed:  # (files already gone, as after a run that stopped midway, are not counted)
            tally['versions'] += 1
            tally['files'] += removed
        self.log('removed older version %s (%s)' % (entry.get('key'), plural(removed, 'file')))
        self.record(entry)

    def links(self, file, entry):
        """Record a design's links on its entry: references (the linked designs, by id and name),
        has_references and references_out_of_date. Read from the cloud file in hand, never by
        opening anything; reading them never stops, fails or skips an export (unknown is None).
        When Fusion raises on reading them, references_unreadable is true and
        references_error holds the first error's text; the design is exported all the same."""
        problems = []
        entry.update(reference_facts(file, problems))
        if hasattr(self.adapter, 'references'):
            try:
                entry['references'] = self.adapter.references(file)
            except Exception as error:
                entry['references'] = None
                problems.append(str(error) or type(error).__name__)
        if entry.get('references') and entry.get('has_references') is None:
            entry['has_references'] = True
        if problems:
            entry.update(references_unreadable=True, references_error=problems[0])
            self.log('could not read the links of ' + entry['key'] + ': ' + problems[0])

    def export_design(self, hub, project, folders, file, entry, key, owned=()):
        """Write a design's .f3d and (when self.step) its STEP (two attempts); the Trouble that stops the
        run, or None. A STEP Fusion would not write leaves the .f3d done, with a warning.

        A design with links (see links()) that fails to open or export gets the plain reason
        link_reason() in error, and Fusion's own message in fusion_error: the usual cause is a
        linked design that was deleted, moved or cannot be read. The export is attempted as
        for any design; only the reason differs."""
        self.links(file, entry)
        for attempt in range(2):
            entry['attempts'] = attempt + 1
            began = self.clock()
            for fact in ('linked_occurrences', 'linked_copy', 'links_broken', 'step', 'step_error', 'step_note',
                         'step_includes_hidden', 'step_hidden_error', 'surface_bodies', 'mesh_bodies'):
                entry.pop(fact, None)  # an earlier attempt's (or run's)
            try:
                stem, facts = self.destination(hub, project, folders, file, owned=owned)
                entry.update(facts)
                written = self.export_checked(file, stem, entry)
                roles = [(path, design_role(path)) for path in written]
                archives = [path for path, role in roles if role == ROLE_ARCHIVE]
                steps = [path for path, role in roles if role == ROLE_STEP and self.step]
                linked = [path for path, role in roles if role == ROLE_LINKED]
                pictures = [path for path, role in roles if role == ROLE_THUMBNAIL]
                details = dict(getattr(self.adapter, 'details', None) or {})
                step_error = details.pop('step_error', None)
                # Quick checks now: a broken archive is retried; a STEP missing bodies is a warning
                # (an empty one of a design with no solid bodies only a note); a STEP Fusion would
                # not write (or wrote incomplete) leaves the .f3d standing.
                facts = checks.inline(archives)
                if steps:
                    try:
                        more = checks.inline(steps, details)
                    except checks.BadExport as error:
                        step_error = str(error)
                        for path in steps:
                            try:
                                os.unlink(long_path(path))
                            except OSError:
                                pass
                        steps = []
                    else:
                        facts['step_solids'] = more['step_solids']
                        facts['warnings'] += more['warnings']
                        if more.get('step_note'):
                            facts['step_note'] = more['step_note']
                elif self.step and step_error is None:
                    step_error = 'Fusion wrote no STEP file'
                paths = archives + steps
                if not self.step:
                    facts['step'] = STEP_OFF
                elif step_error is not None:
                    facts.update(step=STEP_FAILED, step_error=step_error)
                    facts['warnings'].append(STEP_FAILED_WARNING)
                    self.log('STEP not written ' + key + ': ' + step_error)
                linked = self.check_linked(linked, details)
                # Files get the design's date first, then are recorded with that time.
                self.set_times(paths + linked + pictures, file)
                entry['files'] = [dict(file_info(Path(path), self.root), role=design_role(path))
                                  for path in paths + linked]
                entry.pop('thumbnail', None)
                if pictures:
                    entry['thumbnail'] = dict(file_info(Path(pictures[0]), self.root), role=ROLE_THUMBNAIL)
                entry.update(details)
                entry.update(facts)
                entry.update(status='done', error=None)
                entry.pop('fusion_error', None)
                self.mark_long_path(entry)
                return self.alarm
            except NonDesign as error:
                if self.error_callback:
                    self.error_callback(key, error)
                forget_frames(error)
                entry.update(status='skipped', error=str(error))
                entry.pop('fusion_error', None)
                return self.alarm
            except Exception as error:
                if self.error_callback:
                    self.error_callback(key, error)
                forget_frames(error)
                entry.update(status='failed', error=str(error), files=[])
                entry.pop('fusion_error', None)
                self.last_error = str(error)
                self.log('attempt failed ' + key + ': ' + str(error))
                if self.alarm is not None:
                    return self.alarm  # never tried again: the run stops
                trouble = self.trouble(error)
                if trouble is not None:
                    return trouble  # a second attempt cannot succeed either
                if entry.get('has_references') and not isinstance(error, (OpenInFusion, ValueError)):
                    entry.update(error=link_reason(len(entry.get('references') or [])), fusion_error=str(error))
                if self.clock() - began > NO_RETRY_SECONDS:
                    # Another attempt would block Fusion as long again; the next run tries again.
                    self.log('not retried in this run (the attempt took %d s) %s' % (self.clock() - began, key))
                    break
        return None

    def longest_suffix(self):
        """The longest suffix a design's stem gets in this run, for the path-length checks: the
        experimental copy's only while it is on, so the default layout is as before it existed."""
        return LINKED_SUFFIX if self.linked_copies else '.step'

    def export_checked(self, file, stem, entry):
        """adapter.export(file, stem), guarded: when the adapter removed links in memory
        (details['links_broken']), the cloud file's version numbers and date (cloud_state, read
        before and after) must be the same; if not, the entry gets account_changed: true, the log
        an error, and self.alarm the Trouble that stops the run. Nothing is ever saved, so this
        should never happen; if it does, the run must not go on."""
        for name in ('linked_copies', 'step'):
            try:
                setattr(self.adapter, name, getattr(self, name))
            except AttributeError:
                pass
        before = cloud_state(file)
        try:
            return self.adapter.export(file, stem)
        finally:
            details = getattr(self.adapter, 'details', None) or {}
            if details.get('links_broken'):
                after = cloud_state(file)
                if before is not None and after is not None and tuple(after) != tuple(before):
                    entry['account_changed'] = True
                    self.alarm = Trouble('account_changed', ACCOUNT_CHANGED)
                    self.log('ERROR account changed after removing links in memory %s: before %s, after %s'
                             % (entry.get('key'), json.dumps(list(before)), json.dumps(list(after))))
                    if self.error_callback:
                        self.error_callback(entry.get('key'), RuntimeError(ACCOUNT_CHANGED))

    def check_linked(self, linked, details):
        """The copy with linked parts, if it opens as an archive; a broken one is removed and the
        reason recorded (the design's own files stand)."""
        kept = []
        for path in linked:
            try:
                problems, _, _ = checks.archive_facts(path, full=False)
            except OSError as error:
                problems = ['archive is not a zip: ' + str(error)]
            broken = [problem for problem in problems if problem.startswith('archive is not a zip')]
            if not broken:
                kept.append(path)
                continue
            try:
                os.unlink(long_path(path))
            except OSError:
                pass
            copy = details.setdefault('linked_copy', {})
            copy.update(status='skipped', reason='The copy with linked parts was damaged: ' + broken[0])
        return kept

    def save_file(self, hub, project, folders, file, entry, entries, owned=()):
        """Save another file (PDF, upload) as Fusion hands it out, under its own name.
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
            forget_frames(error)
            entry.update(status='skipped', error=str(error), files=[])
            return None
        except Exception as error:
            if self.error_callback:
                self.error_callback(entry['key'], error)
            forget_frames(error)
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
    """The design is open in Fusion in a way that stops its export (unsaved edits,
    another version); the message says what to do. Not a link problem."""


class NotSaved(Exception):
    """Fusion did not hand out another file (not a design); the message says why."""
