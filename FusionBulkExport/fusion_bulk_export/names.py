"""File and folder names on disk: the real names from Fusion, made safe for Windows and macOS.

- clean(): keeps Unicode (NFC), replaces only characters Windows or macOS cannot store,
  trims trailing dots and spaces, guards reserved Windows names and caps a name at 100
  characters.
- Names: decides the name of each folder and file inside one output directory. Folders,
  a design's files and other files share one namespace of full names; a short suffix ("~1a2b",
  from the item's identity) is added only when two different items would get the same name,
  compared without regard to case. The choices are rebuilt from the manifest
  on every run, so an item keeps its name from run to run.
- long_path(): Windows' 260-character limit is avoided with the "\\\\?\\" prefix for this
  add-in's own file access. Fusion itself is never handed a long or prefixed path (it hangs on
  one): fusion_path() refuses them, and Fusion writes to short staging paths (staging.py).
"""
import hashlib
import json
import re
import sys
import unicodedata

NAME_LIMIT = 100        # characters in one cleaned name (a design's stem before " v3.f3d")
WINDOWS_NAME_LIMIT = 64  # on Windows: typical layouts then stay under its 260-character paths
NAME_BYTES = 200        # and UTF-8 bytes, since macOS allows 255 bytes per name
ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
RESERVED = {'CON', 'PRN', 'AUX', 'NUL'} | {'COM%d' % i for i in range(1, 10)} | {'LPT%d' % i for i in range(1, 10)}
# Where a design's older versions are moved once its newest is saved (core.OLDER_MOVE, the default):
# a folder at the top of the output folder, holding them under their own relative paths.
OLDER_FOLDER = 'Older versions'
# Where the CAM tool libraries go (toollibs.py): a folder at the top of the output folder, one .json
# file per library under "<location>/<folders>/". Never a hub's folder name, like OLDER_FOLDER.
TOOL_FOLDER = 'Tool libraries'
# Files the add-in and its command line keep in the output folder: never a hub's folder name.
ROOT_FILES = ('manifest.json', 'manifest.json.tmp', 'manifest.journal', 'export.log', 'autorun.log',
              'autorun-result.json', 'inventory.json', 'diagnostics.json', 'STOP', '.write-check',
              'index.html', 'index.csv', '.staging', 'export.lock', OLDER_FOLDER, TOOL_FOLDER)

WINDOWS_LIMIT = 259     # characters in a full path without the long-path prefix (and Fusion's limit)
# On Windows the output folder itself may be at most this long: below it the files keep their
# names (paths past 260 characters are written with the long-path prefix and reported), but a
# longer root leaves too little room for even the shortened layout, and Windows' own tools stop
# being able to open the folder.
OUTPUT_ROOT_LIMIT = 200
WINDOWS_LONG_LIMIT = 32000
MAC_LIMIT = 1023        # bytes in a full path on macOS
OTHER_LIMIT = 4095


def output_root_problem(path, platform=None):
    """Why this output folder cannot be used (one plain sentence and what to do), or None. Only
    Windows limits it: OUTPUT_ROOT_LIMIT characters."""
    platform = sys.platform if platform is None else platform
    length = len(str(path or ''))
    if platform != 'win32' or length <= OUTPUT_ROOT_LIMIT:
        return None
    return ('The output folder\'s path is %d characters long; on Windows it can be at most %d. '
            'Choose a shorter folder, such as C:\\FusionExport.' % (length, OUTPUT_ROOT_LIMIT))


def clean(value, limit=NAME_LIMIT):
    """One readable, portable file or folder name for a Fusion name."""
    value = unicodedata.normalize('NFC', str(value))
    value = ILLEGAL.sub('_', value).strip().rstrip(' .')
    if value.split('.')[0].rstrip(' ').upper() in RESERVED:
        value = '_' + value
    value = shorten(value, limit)
    return value or '_'


def shorten(value, limit=NAME_LIMIT):
    if len(value) > limit:
        value = value[:limit]
    while len(value.encode('utf-8')) > NAME_BYTES:
        value = value[:-1]
    return value.rstrip(' .')


def tag(identity, length=4):
    return hashlib.sha256(str(identity).encode('utf-8')).hexdigest()[:length]


def fold(name):
    """Comparison key: Windows and macOS (by default) ignore case."""
    return unicodedata.normalize('NFC', name).casefold()


# The second archive of an assembly with linked components, written only while the testing switch
# core.LINKED_COPIES_ENV is on: "<stem> v<version> (with linked parts, experimental).f3d", a copy
# whose links were removed in memory so the linked parts' geometry is inside. Not offered to users:
# such copies crashed Fusion when opened in tests on real Fusion. The suffix comes after the
# design's cleaned (and, on Windows, capped) name, so it is never cut.
LINKED_PARTS = ' (with linked parts, experimental)'
LINKED_SUFFIX = LINKED_PARTS + '.f3d'
# A design's files: "<stem> v<version>" plus these. All are claimed together, so an uploaded
# "Bracket v3.step" or "Bracket v3.png" and a design's export can never share a path.
DESIGN_SUFFIXES = ('.f3d', '.step', '.png', LINKED_SUFFIX)


def design_suffixes(version):
    return [' v%d%s' % (int(version), suffix) for suffix in DESIGN_SUFFIXES]


def entry_names(entry):
    """The full file names a manifest entry (schema 2) uses in its folder: those of its recorded
    files and thumbnail, and those its base name stands for (also for a failed entry)."""
    location = entry.get('location')
    names = []
    for info in list(entry.get('files') or []) + [entry.get('thumbnail') or {}]:
        folder, _, name = str(info.get('path') or '').rpartition('/')
        if name and folder == location:
            names.append(name)
    base = entry.get('base')
    if base is not None:
        if entry.get('kind') == 'file':
            extension = entry.get('extension')
            names.append(base + ('.' + extension if extension else ''))
        elif entry.get('version') is not None:
            names += [base + suffix for suffix in design_suffixes(entry['version'])]
    return names


class Names:
    """The names taken in each output directory: one namespace per directory for everything in
    it (folders, design archives, STEP files, thumbnails, other files), by full name compared
    without regard to case, as Windows and macOS do."""
    def __init__(self):
        self.taken = {}  # directory -> {folded full name: identity}
        for name in ROOT_FILES:
            self.taken.setdefault('', {})[fold(name)] = 'reserved'

    def claim(self, directory, name, identity):
        """A folder's name in directory: name itself, or name~tag if another item has it."""
        return self.claim_group(directory, name, [''], identity)

    def claim_group(self, directory, base, suffixes, identity, free=None):
        """The base name for identity such that every base + suffix in directory is taken by no
        other item (and free(full name) is true, when given: the disk check). base itself, else
        base~tag with a tag from the identity. All the names are then taken for identity."""
        taken = self.taken.setdefault(directory, {})
        # Room for the longest suffix when a name is shortened for its tag. The linked-parts copy's
        # suffix is left out of it: counting it would change the tagged names of designs exported
        # before it existed (names stay within the per-name limits with it all the same).
        longest = max([len(suffix) for suffix in suffixes if not suffix.endswith(LINKED_SUFFIX)] or [0])
        for length in (0, 4, 8, 64):
            candidate = base if not length else (
                shorten(base, max(NAME_LIMIT - longest - length - 1, 1)) + '~' + tag(identity, length))
            full = [candidate + suffix for suffix in suffixes]
            if all(taken.get(fold(name)) in (None, identity) for name in full) and \
                    (free is None or all(free(name) for name in full)):
                for name in full:
                    taken[fold(name)] = identity
                return candidate
        raise ValueError('No free name for ' + json.dumps(base, ensure_ascii=False))

    def directory(self, segments):
        """segments: [(cleaned name, identity), ...] from the output root down. Returns the
        relative directory as a list of names."""
        directory = []
        for name, identity in segments:
            directory.append(self.claim('/'.join(directory), name, identity))
        return directory

    def remember(self, entry):
        """Take the names a manifest entry (schema 2) already uses."""
        location, identities = entry.get('location'), entry.get('identities')
        if entry.get('shortened') or location is None or not identities or entry.get('base') is None:
            return
        parts = [part for part in location.split('/') if part]
        if len(parts) != len(identities):
            return
        for index, (name, identity) in enumerate(zip(parts, identities)):
            self.taken.setdefault('/'.join(parts[:index]), {}).setdefault(fold(name), identity)
        taken = self.taken.setdefault('/'.join(parts), {})
        for name in entry_names(entry):
            taken.setdefault(fold(name), 'file:' + str(entry['id']))


def absolute_path(path, pathmodule=None):
    """path made absolute without following links or drives: a mapped network drive keeps its
    letter (X:\\Exports, never the \\\\server\\share it maps to, as Path.resolve() gives on
    Windows), a symbolic link keeps its own path, "." and ".." are folded. pathmodule: os.path
    (default), or ntpath / posixpath in tests."""
    import os
    pathmodule = pathmodule or os.path
    return pathmodule.normpath(pathmodule.abspath(pathmodule.expanduser(str(path))))


def is_unc(path):
    """Whether path is a Windows network path (\\\\server\\share\\...), not a drive letter."""
    text = str(path).replace('/', '\\')
    return text.startswith('\\\\') and not text.startswith('\\\\?\\') and not text.startswith('\\\\.\\')


def windows_length(path):
    return len(str(path).encode('utf-16-le')) // 2


def long_path(path, windows=None):
    """path as a string for file-system calls; on Windows with the \\\\?\\ prefix so that
    paths longer than 260 characters work. Relative or already prefixed paths are unchanged."""
    windows = sys.platform == 'win32' if windows is None else windows
    text = str(path)
    if not windows:
        return text
    text = text.replace('/', '\\')
    if text.startswith('\\\\?\\') or not (text.startswith('\\\\') or re.match(r'^[A-Za-z]:\\', text)):
        return text
    if text.startswith('\\\\'):
        return '\\\\?\\UNC\\' + text[2:]
    return '\\\\?\\' + text


def fusion_path(path, windows=None):
    """The path string handed to Fusion. Fusion hangs when given a path over 260 characters on
    Windows (even with the \\\\?\\ prefix), so a long or prefixed path is refused here; Fusion
    writes to a short staging path instead (staging.py)."""
    text = str(path)
    if text.startswith('\\\\?\\') or windows_length(text) > WINDOWS_LIMIT:
        raise ValueError('Path too long to hand to Fusion (%d characters)' % windows_length(text))
    return text


def too_long(path, platform=None):
    """Whether Fusion and the file system can be expected to fail on this full path."""
    platform = sys.platform if platform is None else platform
    if platform == 'win32':
        return windows_length(path) > WINDOWS_LONG_LIMIT
    return len(str(path).encode('utf-8')) > (MAC_LIMIT if platform == 'darwin' else OTHER_LIMIT)
