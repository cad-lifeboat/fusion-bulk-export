"""File and folder names on disk: the real names from Fusion, made safe for Windows and macOS.

- clean(): keeps Unicode (NFC), replaces only characters Windows or macOS cannot store,
  trims trailing dots and spaces, guards reserved Windows names and caps a name at 100
  characters.
- Names: decides the name of each folder and design inside one output directory. A short
  suffix ("~1a2b", from the item's identity) is added only when two different items would get
  the same name, compared without regard to case. The choices are rebuilt from the manifest
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
NAME_BYTES = 200        # and UTF-8 bytes, since macOS allows 255 bytes per name
ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
RESERVED = {'CON', 'PRN', 'AUX', 'NUL'} | {'COM%d' % i for i in range(1, 10)} | {'LPT%d' % i for i in range(1, 10)}
# Files the add-in and its command line keep in the output folder: never a hub's folder name.
ROOT_FILES = ('manifest.json', 'manifest.json.tmp', 'manifest.journal', 'export.log', 'autorun.log',
              'autorun-result.json', 'inventory.json', 'diagnostics.json', 'STOP', '.write-check',
              'index.html', 'index.csv', '.staging')

WINDOWS_LIMIT = 259     # characters in a full path without the long-path prefix (and Fusion's limit)
WINDOWS_LONG_LIMIT = 32000
MAC_LIMIT = 1023        # bytes in a full path on macOS
OTHER_LIMIT = 4095


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


class Names:
    """Names taken in each output directory, by kind ('d' folder, 'f' design stem)."""
    def __init__(self):
        self.taken = {}  # directory -> {(kind, folded name): identity}
        for name in ROOT_FILES:
            self.taken.setdefault('', {})[('d', fold(name))] = 'reserved'

    def claim(self, directory, kind, name, identity):
        """The name for identity in directory: name itself, or name~tag if another item has it."""
        taken = self.taken.setdefault(directory, {})
        for length in (0, 4, 8, 64):
            candidate = name if not length else shorten(name, NAME_LIMIT - length - 1) + '~' + tag(identity, length)
            owner = taken.get((kind, fold(candidate)))
            if owner is None:
                taken[(kind, fold(candidate))] = identity
                return candidate
            if owner == identity:
                return candidate
        raise ValueError('No free name for ' + json.dumps(name, ensure_ascii=False))

    def place(self, segments, stem, file_identity, kind='f'):
        """segments: [(cleaned name, identity), ...] from the output root down. Returns the
        relative directory (a list of names) and the item's stem name. kind is 'f' for a design's
        stem; other files use file_kind(extension), so only names with the same extension compete."""
        directory = []
        for name, identity in segments:
            directory.append(self.claim('/'.join(directory), 'd', name, identity))
        return directory, self.claim('/'.join(directory), kind, stem, file_identity)

    def remember(self, entry):
        """Take the names a manifest entry (schema 2) already uses."""
        location, identities = entry.get('location'), entry.get('identities')
        if entry.get('shortened') or location is None or not identities or entry.get('base') is None:
            return
        parts = [part for part in location.split('/') if part]
        if len(parts) != len(identities):
            return
        for index, (name, identity) in enumerate(zip(parts, identities)):
            self.taken.setdefault('/'.join(parts[:index]), {}).setdefault(('d', fold(name)), identity)
        kind = file_kind(entry.get('extension')) if entry.get('kind') == 'file' else 'f'
        self.taken.setdefault('/'.join(parts), {}).setdefault((kind, fold(entry['base'])), 'file:' + entry['id'])


def file_kind(extension):
    """The Names kind of a saved file with this extension (a design's stem is 'f')."""
    return 'f.' + fold(extension) if extension else 'f.'


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
