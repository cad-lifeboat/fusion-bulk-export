"""CAM tool libraries: each of the user's own tool libraries (Manufacture > Tool Library) saved as
one .json file, once per run, at its start (they are quick), before any design:

    <export folder>/Tool libraries/<location>/<folders...>/<library>.json

Read through Fusion's CAM API, documented as
adsk.cam.CAMManager.get().libraryManager.toolLibraries (ToolLibraries): urlByLocation() for each
location (adsk.cam.LibraryLocations), childFolderURLs() and childAssetURLs() to walk its folders,
toolLibraryAtURL() for a library and ToolLibrary.toJson() for its contents. Only those are called:
nothing that writes to a library. Any of them may be missing in an older Fusion or raise: a CAM
API that is missing or unusable is a warning (run_info["tool_libraries"]["warning"]), never a
failure of the export, and a library whose contents cannot be read is that library failed only.

The user's own locations (LOCATIONS: Local, Cloud, Hub, Network, External) are exported;
Autodesk's own sample libraries (Fusion360 and OnlineSamples) are skipped.

manifest.json gets a top-level "tool_libraries" list, one entry per library ever exported into
the folder: key (a hash of the library's URL; the URL itself is never written), location,
url_scheme, folder, name, path, tools, bytes, sha256, mtime_ns, exported (when the file was
written), seen (the start of the last run that found the library in Fusion), status ("done" or
"failed") and error; older: what happened to its previous copies.

On a later run a library whose .json is the same (SHA-256) is up to date and not written again.
A changed one replaces its file; the previous copy is handled by the "Older versions of a design"
choice (core.OLDER_VERSIONS), and only while it still matches its record:
  move (default): to "Older versions/Tool libraries/..." with its exported date in the name,
                  "Mills (2026-11-20).json";
  keep:           beside the new one under that dated name;
  delete:         deleted (replaced).
A file that changed since it was exported (the user's own edits) is never written over or moved:
the new copy then gets a name of its own ("Mills~1a2b.json"). A library that is no longer in
Fusion keeps its file and its entry.
"""
import hashlib
import importlib
import json
import os
from pathlib import Path
from urllib.parse import unquote

from . import checks
from .names import OLDER_FOLDER, TOOL_FOLDER, Names, clean, long_path
from .words import plural

# (adsk.cam.LibraryLocations member, its folder in the export, exported). Autodesk's own sample
# libraries are not the user's: skipped. On real Fusion (2706, macOS) Local was toollibraryroot://,
# Cloud cloud://, Hub hub://, External custom://, Fusion360 systemlibraryroot:// (Autodesk's
# samples); Network and OnlineSamples both gave a URL with no scheme over the same 29 folders of
# samples, so Network is skipped like them.
LOCATIONS = (
    ('LocalLibraryLocation', 'Local', True),
    ('CloudLibraryLocation', 'Cloud', True),
    ('HubLibraryLocation', 'Hub', True),
    ('ExternalLibraryLocation', 'External', True),
    ('NetworkLibraryLocation', 'Network', False),
    ('Fusion360LibraryLocation', 'Fusion samples', False),
    ('OnlineSamplesLibraryLocation', 'Online samples', False),
)
SAMPLES = tuple(label for _, label, own in LOCATIONS if not own)
MAX_DEPTH = 32
# What Fusion raises for a location that holds nothing (seen for cloud and hub locations): no
# libraries there, not a problem.
NO_FOLDER = 'does not point to an existing folder'              # folders below a location's root; deeper ones are not walked
LIBRARY_SUFFIXES = ('.json', '.tools', '.hsmlib')  # taken off a library's URL leaf name
EXTENSION = '.json'
# False: the file holds toJson()'s text exactly as Fusion gave it. True: re-indented (2 spaces).
PRETTY = False
KIND = 'tool_library'

NO_CAM = "This Fusion offers add-ins no access to tool libraries (its CAM API is missing)."
KEPT_EDITED = 'changed since it was exported, so it was left as it is; the new copy has a name of its own'


class Unavailable(Exception):
    """The CAM API cannot be used in this Fusion: str() is the plain sentence saying why (a warning);
    detail, when given, is Fusion's or Python's own error (logged only)."""
    def __init__(self, reason, detail=None):
        super().__init__(reason)
        self.detail = detail


def text_of(error):
    return str(error) or type(error).__name__


def empty_location(error):
    """Whether Fusion's error only says there is nothing at that URL (NO_FOLDER)."""
    return NO_FOLDER in text_of(error).lower()


def load_cam():
    """The adsk.cam module (Fusion's CAM API); raises Unavailable when this Fusion has none."""
    try:
        return importlib.import_module('adsk.cam')
    except Exception as error:
        raise Unavailable(NO_CAM, text_of(error))


def tool_libraries_api(cam):
    """cam's ToolLibraries object, checked for the members used here; raises Unavailable."""
    manager_class = getattr(cam, 'CAMManager', None)
    if manager_class is None or not hasattr(manager_class, 'get'):
        raise Unavailable(NO_CAM)
    try:
        manager = manager_class.get()
        libraries = manager.libraryManager.toolLibraries if manager is not None else None
    except Exception as error:
        raise Unavailable("Fusion's tool libraries could not be opened.", text_of(error))
    if libraries is None:
        raise Unavailable("Fusion's tool libraries could not be opened.", 'Fusion returned none')
    missing = [name for name in ('urlByLocation', 'childFolderURLs', 'childAssetURLs', 'toolLibraryAtURL')
               if not hasattr(libraries, name)]
    if missing:
        raise Unavailable("This Fusion's CAM API lacks " + ', '.join(missing) + '.')
    if getattr(cam, 'LibraryLocations', None) is None:
        raise Unavailable("This Fusion's CAM API lacks LibraryLocations.")
    return libraries


def listed(value):
    """A list from what Fusion returns: a list or tuple, a collection (count/item), or None."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    if hasattr(value, 'count') and hasattr(value, 'item'):
        return [value.item(index) for index in range(int(value.count))]
    return list(value)


def url_text(url):
    """A URL object's text (adsk.core.URL.toString()), or the string itself."""
    if isinstance(url, str):
        return url
    to_string = getattr(url, 'toString', None)
    if to_string is not None:
        return str(to_string())
    return str(url)


def leaf_name(url):
    """The last part of a URL (adsk.core.URL.leafName where there is one), unquoted."""
    try:
        leaf = getattr(url, 'leafName', None)
    except Exception:
        leaf = None
    if not leaf:
        leaf = unquote(url_text(url).rstrip('/').rsplit('/', 1)[-1])
    return str(leaf)


def library_name(url):
    """A library's name from its URL: its leaf name without a library file extension."""
    name = leaf_name(url)
    for suffix in LIBRARY_SUFFIXES:
        if name.lower().endswith(suffix) and len(name) > len(suffix):
            return name[:-len(suffix)]
    return name


def scheme(url):
    """Only the scheme of a URL ("systemlibraryroot", "cloud"...): the rest can hold ids."""
    text = url_text(url)
    return text.split('://', 1)[0] if '://' in text else ''


def library_key(url):
    """A library's identity from run to run: a hash of its URL (the URL itself is not kept)."""
    return hashlib.sha256(url_text(url).encode('utf-8')).hexdigest()[:24]


class Found:
    """A library found in Fusion: where (location label and folder names), its name and URL."""
    def __init__(self, location, folder, url, scheme_name):
        self.location, self.folder, self.url = location, list(folder), url
        self.name = library_name(url)
        self.scheme = scheme_name
        self.key = library_key(url)


def find(cam, libraries, problems, log=lambda message: None):
    """Yield every library (Found) in the user's own locations, folder by folder. A location
    or folder Fusion will not list is added to problems ({"location", "error"}) and skipped."""
    locations = getattr(cam, 'LibraryLocations', None)
    seen = set()
    for member, label, own in LOCATIONS:
        if not own:
            continue
        value = getattr(locations, member, None)
        if value is None:
            log('tool libraries: this Fusion has no location ' + member)
            continue
        try:
            root = libraries.urlByLocation(value)
        except Exception as error:
            if empty_location(error):
                log('tool libraries: nothing in location ' + label)
                continue
            problems.append(dict(location=label, error=text_of(error)))
            log('tool libraries: location %s could not be read: %s' % (label, text_of(error)))
            continue
        if root is None or not url_text(root):
            continue
        yield from walk(libraries, root, label, [], seen, problems, log)


def walk(libraries, url, label, folder, seen, problems, log, depth=0):
    if url_text(url) in seen:
        return
    seen.add(url_text(url))
    try:
        assets = listed(libraries.childAssetURLs(url))
    except Exception as error:
        if empty_location(error):
            return
        problems.append(dict(location=label, error=text_of(error)))
        log('tool libraries: a folder in %s could not be listed: %s' % (label, text_of(error)))
        assets = []
    for asset in assets:
        if url_text(asset) in seen:
            continue
        seen.add(url_text(asset))
        yield Found(label, folder, asset, scheme(asset))
    if depth >= MAX_DEPTH:
        return
    try:
        children = listed(libraries.childFolderURLs(url))
    except Exception as error:
        if empty_location(error):
            return
        problems.append(dict(location=label, error=text_of(error)))
        log('tool libraries: the folders of a folder in %s could not be listed: %s' % (label, text_of(error)))
        children = []
    for child in children:
        yield from walk(libraries, child, label, folder + [leaf_name(child)], seen, problems, log, depth + 1)


def library_json(libraries, found):
    """(bytes to write, tool count) of a library; raises when Fusion will not hand it out."""
    library = libraries.toolLibraryAtURL(found.url)
    if library is None:
        raise ValueError('Fusion returned no tool library for it.')
    text = library.toJson()
    if text is None or text == '':
        raise ValueError('Fusion returned no contents for it.')
    data = text if isinstance(text, bytes) else str(text).encode('utf-8')
    parsed = None
    try:
        parsed = json.loads(data.decode('utf-8'))
    except ValueError:
        pass
    if PRETTY and parsed is not None:
        data = (json.dumps(parsed, indent=2, ensure_ascii=False) + '\n').encode('utf-8')
    return data, tool_count(library, parsed)


def tool_count(library, parsed=None):
    """How many tools a library holds: ToolLibrary.count, else the length of the JSON's "data"
    list (the layout of Fusion's tool library files), else None."""
    try:
        count = library.count
        if isinstance(count, int) and not isinstance(count, bool):
            return count
    except Exception:
        pass
    if isinstance(parsed, dict) and isinstance(parsed.get('data'), list):
        return len(parsed['data'])
    return None


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def dated_name(name, exported):
    """"Mills.json" exported on 2026-11-20 -> "Mills (2026-11-20).json"."""
    stem, extension = os.path.splitext(name)
    day = str(exported or '')[:10] or 'earlier'
    return '%s (%s)%s' % (stem, day, extension)


def numbered(path, number):
    stem, extension = os.path.splitext(path.name)
    return path.with_name('%s (%d)%s' % (stem, number, extension))


def new_tally():
    return dict(found=0, exported=0, up_to_date=0, failed=0, tools_exported=0, tools_up_to_date=0, moved=0,
                kept=0, removed=0, unreadable=[], warning=None, skipped_locations=list(SAMPLES), listed=False)


class ToolLibraryExport:
    """The tool library part of one run of an Exporter (core.Exporter): writes the files, keeps
    manifest["tool_libraries"] and run_info["tool_libraries"] (new_tally())."""
    def __init__(self, exporter):
        self.exporter = exporter
        self.root = exporter.root
        self.entries = exporter.manifest.setdefault('tool_libraries', [])
        self.by_key = {entry.get('key'): entry for entry in self.entries if isinstance(entry, dict)}
        self.tally = exporter.run_info['tool_libraries'] = new_tally()
        self.names = Names()  # names below TOOL_FOLDER (its own namespace)
        for entry in self.entries:
            self.remember(entry)

    def log(self, message):
        self.exporter.log(message)

    @staticmethod
    def segments(location, folder, limit):
        """[(cleaned name, identity)] from TOOL_FOLDER down to a library's folder."""
        out = [(clean(location, limit), 'location:' + location)]
        for index, name in enumerate(folder):
            out.append((clean(name, limit), 'folder:' + json.dumps([location] + list(folder[:index + 1]),
                                                                    ensure_ascii=False)))
        return out

    def remember(self, entry):
        """Take the names an entry's file (and its kept dated copies) already use."""
        path = str(entry.get('path') or '')
        parts = path.split('/')
        if len(parts) < 3 or parts[0] != TOOL_FOLDER:
            return
        directories, name = parts[1:-1], parts[-1]
        identities = [identity for _, identity in self.segments(str(entry.get('location') or ''),
                                                                list(entry.get('folder') or []), 1000)]
        if len(identities) == len(directories):
            for index, (part, identity) in enumerate(zip(directories, identities)):
                self.names.taken.setdefault('/'.join(directories[:index]), {}).setdefault(part.casefold(), identity)
        taken = self.names.taken.setdefault('/'.join(directories), {})
        owner = 'library:' + str(entry.get('key'))
        taken.setdefault(name.casefold(), owner)
        for older in entry.get('older') or []:
            kept = str(older.get('kept_as') or '')
            if kept.startswith(TOOL_FOLDER + '/'):
                kept_parts = kept.split('/')
                self.names.taken.setdefault('/'.join(kept_parts[1:-1]), {}).setdefault(kept_parts[-1].casefold(), owner)

    def run(self, cam=None, cancelled=lambda: False, progress=None):
        """Export every library; returns the core.Trouble that must stop the run (a full disk, an
        output folder gone), else None. Never raises for anything Fusion does."""
        try:
            cam = load_cam() if cam is None else cam
            libraries = tool_libraries_api(cam)
        except Unavailable as error:
            self.tally['warning'] = str(error)
            self.log('tool libraries not exported: ' + str(error) + (' (%s)' % error.detail if error.detail else ''))
            return None
        started = self.exporter.run_info.get('started')
        listing = find(cam, libraries, self.tally['unreadable'], self.log)
        number = 0
        while True:
            try:
                found = next(listing)
            except StopIteration:
                self.tally['listed'] = not self.tally['unreadable']
                break
            except Exception as error:  # a listing that fails in a way not caught where it happened
                self.tally['unreadable'].append(dict(location='', error=text_of(error)))
                self.log('tool libraries: listing stopped: ' + text_of(error))
                break
            if cancelled():
                break
            number += 1
            self.tally['found'] += 1
            if progress is not None:
                progress(number, found)
            trouble = self.one(libraries, found, started)
            if trouble is not None:
                return trouble
        self.log('tool libraries: %d found, %d exported, %d up to date, %d failed'
                 % (self.tally['found'], self.tally['exported'], self.tally['up_to_date'], self.tally['failed']))
        return None

    def one(self, libraries, found, started):
        """Export one library. Returns a core.Trouble when the output folder cannot be written."""
        from .core import now
        entry = self.by_key.get(found.key)
        if entry is None:
            entry = dict(key=found.key, kind=KIND)
            self.entries.append(entry)
            self.by_key[found.key] = entry
        entry.update(kind=KIND, location=found.location, url_scheme=found.scheme, folder=list(found.folder),
                     name=found.name, seen=started)
        try:
            data, tools = library_json(libraries, found)
        except Exception as error:
            entry.update(status='failed', error=text_of(error))
            self.tally['failed'] += 1
            self.log('tool library failed: ' + text_of(error))
            return None
        try:
            written = self.store(entry, found, data)
        except OSError as error:
            entry.update(status='failed', error=text_of(error))
            self.tally['failed'] += 1
            self.log('tool library not saved: ' + text_of(error))
            return self.exporter.trouble(error)
        entry.update(status='done', error=None, tools=tools)
        if written:
            entry['exported'] = now()
            self.tally['exported'] += 1
            self.tally['tools_exported'] += tools or 0
        else:
            self.tally['up_to_date'] += 1
            self.tally['tools_up_to_date'] += tools or 0
        return None

    def on_disk(self, path, info):
        """The SHA-256 of the file at path now (the recorded one when its size and time match the
        record), or None when there is no such plain file."""
        try:
            stat = os.stat(long_path(path))
        except OSError:
            return None
        if not os.path.isfile(long_path(path)):
            return None
        if info and stat.st_size == info.get('bytes') and stat.st_mtime_ns == info.get('mtime_ns') \
                and info.get('sha256'):
            return info['sha256']
        return checks.file_sha256(path)

    def store(self, entry, found, data):
        """Write data as the library's file, unless it is there already; True when the entry now
        records a new file (written now, or found there with these contents from a run that
        stopped before recording it)."""
        from .core import now
        sha = sha256_bytes(data)
        recorded = entry.get('path')
        if recorded:
            path = self.root / recorded
            there = self.on_disk(path, entry)
            if there == sha:
                if entry.get('sha256') != sha:  # written by a run that stopped before recording it
                    self.record(entry, path)
                return False
            if there is None:
                self.write(path, data)
                self.record(entry, path)
                return True
            if there == entry.get('sha256'):
                self.set_aside(entry, path)
                self.write(path, data)
                self.record(entry, path)
                return True
            # Changed since it was exported (edited by hand?): never written over or moved; the
            # new copy gets a name of its own beside it.
            entry.setdefault('older', []).append(dict(path=recorded, sha256=entry.get('sha256'),
                                                      bytes=entry.get('bytes'), exported=entry.get('exported'),
                                                      date=now(), left=KEPT_EDITED))
            self.log('tool library: its earlier file changed since it was exported; left as it is')
        path = self.claim(found, sha, avoid=recorded)
        if self.on_disk(path, None) != sha:
            self.write(path, data)
        self.record(entry, path)
        return True

    def claim(self, found, sha, avoid=None):
        """A free path for a library: its own name, or name~tag when another file or library has
        it. A file there with the same contents counts as free (a stopped run wrote it)."""
        limit = self.exporter.name_limit
        parts = self.names.directory(self.segments(found.location, found.folder, limit))
        directory = self.root.joinpath(TOOL_FOLDER, *parts)
        os.makedirs(long_path(directory), exist_ok=True)
        relative_dir = TOOL_FOLDER + '/' + '/'.join(parts)

        def free(name):
            relative = relative_dir + '/' + name
            if avoid and relative.casefold() == avoid.casefold():
                return False
            path = directory / name
            if not os.path.lexists(long_path(path)):
                return True
            return self.on_disk(path, None) == sha

        if avoid:
            # The name this library had is taken by its own edited file: free it for the claim's
            # check (free() still refuses that path), so a tagged name is chosen.
            self.names.taken.get('/'.join(parts), {}).pop(avoid.rsplit('/', 1)[-1].casefold(), None)
        base = self.names.claim_group('/'.join(parts), clean(found.name, limit - len(EXTENSION)), [EXTENSION],
                                      'library:' + found.key, free=free)
        return directory / (base + EXTENSION)

    def write(self, path, data):
        os.makedirs(long_path(path.parent), exist_ok=True)
        temporary = path.with_name(path.name + '.tmp')
        with open(long_path(temporary), 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(long_path(temporary), long_path(path))

    def record(self, entry, path):
        stat = os.stat(long_path(path))
        entry.update(path=path.relative_to(self.root).as_posix(), sha256=checks.file_sha256(path),
                     bytes=stat.st_size, mtime_ns=stat.st_mtime_ns)

    def set_aside(self, entry, path):
        """The previous copy of a changed library (checked unchanged since it was exported), by
        the "Older versions of a design" choice: moved, kept beside under a dated name, or deleted
        (then simply replaced by the new file)."""
        from .core import OLDER_DELETE, OLDER_KEEP, now
        choice = self.exporter.older_versions
        relative = entry['path']
        record = dict(path=relative, sha256=entry.get('sha256'), bytes=entry.get('bytes'),
                      exported=entry.get('exported'), date=now())
        name = dated_name(path.name, entry.get('exported'))
        if choice == OLDER_DELETE:
            record['removed'] = record['date']
            self.tally['removed'] += 1
            self.log('tool library: its previous copy is replaced (Older versions: delete)')
        else:
            if choice == OLDER_KEEP:
                target = path.with_name(name)
            else:
                target = self.root / OLDER_FOLDER / Path(relative).parent / name
            number = 1
            candidate = target
            while os.path.lexists(long_path(candidate)):
                number += 1
                candidate = numbered(target, number)
            os.makedirs(long_path(candidate.parent), exist_ok=True)
            os.replace(long_path(path), long_path(candidate))
            moved_to = candidate.relative_to(self.root).as_posix()
            if choice == OLDER_KEEP:
                record['kept_as'] = moved_to
                self.tally['kept'] += 1
                parts = moved_to.split('/')
                self.names.taken.setdefault('/'.join(parts[1:-1]), {})[parts[-1].casefold()] = 'library:' + entry['key']
            else:
                record['moved_to'] = moved_to
                self.tally['moved'] += 1
            self.log('tool library: its previous copy was %s' % ('kept beside it' if choice == OLDER_KEEP
                                                                 else 'moved to ' + OLDER_FOLDER))
        entry.setdefault('older', []).append(record)


# The words of the summary, the report and the command line.

def summary_notes(tally):
    """The results dialog's lines about tool libraries in this run (none when they were off)."""
    if not tally:
        return []
    notes = []
    if tally.get('warning'):
        notes.append('Tool libraries were not exported. ' + tally['warning'])
        return notes
    exported, up_to_date, failed = tally.get('exported', 0), tally.get('up_to_date', 0), tally.get('failed', 0)
    if exported:
        notes.append('Exported %s (%s).' % (plural(exported, 'tool library'),
                                            plural(tally.get('tools_exported', 0), 'tool')))
    if up_to_date:
        notes.append('%s already up to date.' % plural(up_to_date, 'tool library'))
    changed = []
    for key, words in (('moved', 'moved to ' + OLDER_FOLDER), ('kept', 'kept beside the new one'),
                       ('removed', 'replaced')):
        if tally.get(key):
            changed.append('%s %s' % (plural(tally[key], 'previous copy'), words))
    if changed:
        notes.append('Changed tool libraries: %s.' % ', '.join(changed))
    if failed:
        notes.append('%s could not be exported; the report says why.' % plural(failed, 'tool library'))
    unreadable = len(tally.get('unreadable') or [])
    if unreadable:
        notes.append('%s of tool libraries could not be read.' % plural(unreadable, 'location or folder',
                                                                          'locations or folders'))
    if not (exported or up_to_date or failed or unreadable):
        notes.append('No tool libraries of your own were found.')
    return notes


IMPORT_HINT = ('To use one in Fusion again: Manufacture > Tool Library, right-click a library folder (such as '
               'Local) > Import, then pick the .json file.')


def entries(manifest):
    """The tool library entries of a manifest, by location, folder and name."""
    found = [entry for entry in (manifest or {}).get('tool_libraries') or [] if isinstance(entry, dict)]
    found.sort(key=lambda e: (str(e.get('location') or '').casefold(), '/'.join(e.get('folder') or []).casefold(),
                              str(e.get('name') or '').casefold()))
    return found


def older_text(record):
    """One previous copy as text: "2026-11-20: moved to Older versions/...json"."""
    day = str(record.get('exported') or '')[:10] or 'earlier'
    if record.get('moved_to'):
        return 'Copy of %s moved to %s' % (day, record['moved_to'])
    if record.get('kept_as'):
        return 'Copy of %s kept as %s' % (day, record['kept_as'])
    if record.get('removed'):
        return 'Copy of %s replaced (deleted)' % day
    if record.get('left'):
        return 'Copy of %s at %s: %s' % (day, record.get('path'), record['left'])
    return 'Copy of %s' % day


def entry_notes(entry, run_info=None):
    """Notes on one library for the report: gone from Fusion, kept file, previous copies."""
    notes = []
    tally = (run_info or {}).get('tool_libraries') or {}
    started = (run_info or {}).get('started')
    if tally.get('listed') and started and entry.get('seen') != started:
        notes.append('Not found in Fusion in the last run; its file is kept.')
    notes += [older_text(record) for record in entry.get('older') or []]
    return notes


def count_line(entries_list):
    """'3 tool libraries (412 tools), 1 failed' for the folder's entries."""
    done = [entry for entry in entries_list if entry.get('status') == 'done']
    failed = sum(1 for entry in entries_list if entry.get('status') == 'failed')
    tools = sum(entry.get('tools') or 0 for entry in done)
    line = '%s (%s)' % (plural(len(done), 'tool library'), plural(tools, 'tool'))
    if failed:
        line += ', %d failed' % failed
    return line


def check_entry(root, entry):
    """(problems, line) for one tool library entry of a folder: its file there with the recorded
    size and SHA-256. A failed entry with no file recorded has nothing to check."""
    problems = []
    relative = entry.get('path')
    if not relative:
        return problems, 'not exported yet'
    path = Path(root) / relative
    if not os.path.isfile(long_path(path)):
        return ['file missing: ' + relative], 'tool library'
    size = os.stat(long_path(path)).st_size
    if ('bytes' in entry and size != entry['bytes']) or ('sha256' in entry and checks.file_sha256(path) != entry['sha256']):
        problems.append('file changed since export: ' + relative)
    tools = entry.get('tools')
    return problems, 'tool library, %s' % ('%s' % plural(tools, 'tool') if isinstance(tools, int) else 'tools unknown')

