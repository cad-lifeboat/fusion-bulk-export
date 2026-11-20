"""What the dialog remembers between runs (the last output folder and mode), in the user's
app-data folder, never beside the add-in (which a reinstall replaces):

  macOS:   ~/Library/Application Support/FusionBulkExport/settings.json
  Windows: %APPDATA%\\FusionBulkExport\\settings.json

FUSION_BULK_EXPORT_SETTINGS, when set, names another file (used by the tests).
"""
import json
import os
from pathlib import Path
import sys

from .journal import Journal, replay
from .names import long_path

NAME = 'FusionBulkExport'


def settings_path(platform=None, environ=None, home=None):
    platform = sys.platform if platform is None else platform
    environ = os.environ if environ is None else environ
    if environ.get('FUSION_BULK_EXPORT_SETTINGS'):
        return Path(environ['FUSION_BULK_EXPORT_SETTINGS'])
    home = Path(home) if home is not None else Path.home()
    if platform == 'win32':
        base = Path(environ['APPDATA']) if environ.get('APPDATA') else home / 'AppData' / 'Roaming'
    elif platform == 'darwin':
        base = home / 'Library' / 'Application Support'
    else:
        base = Path(environ['XDG_CONFIG_HOME']) if environ.get('XDG_CONFIG_HOME') else home / '.config'
    return base / NAME / 'settings.json'


def load(path=None):
    """The remembered settings; {} when there are none or the file cannot be read."""
    path = settings_path() if path is None else Path(path)
    try:
        values = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return values if isinstance(values, dict) else {}


def store(values, path=None, drop=()):
    """Merge values into the settings file, removing the keys in drop (settings of earlier
    versions that mean nothing now). Remembering is a convenience: failures are ignored."""
    path = settings_path() if path is None else Path(path)
    merged = load(path)
    merged.update(values)
    for key in drop:
        merged.pop(key, None)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + '.tmp')
        temporary.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        os.replace(str(temporary), str(path))
        return True
    except OSError:
        return False


def default_output(home=None):
    """First-use suggestion: Documents/FusionExport (created only when an export starts)."""
    return str((Path(home) if home is not None else Path.home()) / 'Documents' / 'FusionExport')


def folder_facts(folder):
    """What an output folder already holds, without changing it: dict(state, done, projects,
    error). state is 'new' (no such folder), 'empty' (no manifest), 'continues' or 'unreadable';
    projects maps a project id to how many of its items are exported there."""
    facts = dict(state='new', done=0, projects={}, error=None)
    if not folder:
        return facts
    root = Path(folder)
    if not os.path.isdir(long_path(root)):
        return facts
    manifest_path = root / 'manifest.json'
    if not os.path.exists(long_path(manifest_path)):
        facts['state'] = 'empty'
        return facts
    try:
        with open(long_path(manifest_path), encoding='utf-8') as stream:
            manifest = json.loads(stream.read())
        manifest.setdefault('entries', [])
        replay(manifest, Journal(root / 'manifest.journal').read())
    except (OSError, ValueError, AttributeError, KeyError, TypeError) as error:
        facts.update(state='unreadable', error=str(error))
        return facts
    facts['state'] = 'continues'
    for entry in manifest['entries']:
        if entry.get('status') != 'done':
            continue
        facts['done'] += 1
        try:
            project = str(json.loads(entry['key'])[1])
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        facts['projects'][project] = facts['projects'].get(project, 0) + 1
    return facts


def folder_note(facts):
    """One line under the folder row in the dialog."""
    if facts['state'] == 'continues':
        return 'Continues the export in this folder (%d done).' % facts['done']
    if facts['state'] == 'unreadable':
        return 'This folder\'s manifest.json cannot be read; choose another folder.'
    if facts['state'] == 'empty':
        return 'A new export into this folder.'
    return 'A new export; the folder is created when the export starts.'
