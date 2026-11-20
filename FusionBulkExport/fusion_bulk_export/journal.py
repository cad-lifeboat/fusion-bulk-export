"""manifest.journal: one JSON line per finished item, so manifest.json need not be rewritten
after every design.

The exporter appends each finished entry here (flushed and synced at once) and rewrites the
full manifest.json only now and then. If Fusion or the computer stops mid-run, the next run
replays the journal over manifest.json, saves, and empties the journal. Replaying twice is
harmless: an entry replaces the one with the same key.
"""
import json
import os

from .names import long_path


class Journal:
    def __init__(self, path):
        self.path = path

    def append(self, entry):
        line = json.dumps(dict(entry=entry), ensure_ascii=False, separators=(',', ':'))
        with open(long_path(self.path), 'a', encoding='utf-8') as stream:
            stream.write(line + '\n')
            stream.flush()
            os.fsync(stream.fileno())

    def read(self):
        """Entries in the order written. A line cut short by a crash is ignored."""
        try:
            with open(long_path(self.path), encoding='utf-8') as stream:
                text = stream.read()
        except FileNotFoundError:
            return []
        entries = []
        for line in text.splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and isinstance(record.get('entry'), dict) and 'key' in record['entry']:
                entries.append(record['entry'])
        return entries

    def clear(self):
        """Called after manifest.json has been saved with everything in the journal."""
        try:
            os.unlink(long_path(self.path))
        except FileNotFoundError:
            pass


def replay(manifest, entries):
    """Apply journal entries to manifest['entries'] (the same key is replaced). Returns how many."""
    positions = {entry['key']: index for index, entry in enumerate(manifest['entries'])}
    for entry in entries:
        if entry['key'] in positions:
            manifest['entries'][positions.pop(entry['key'])] = None
        manifest['entries'].append(entry)
        positions[entry['key']] = len(manifest['entries']) - 1
    manifest['entries'] = [entry for entry in manifest['entries'] if entry is not None]
    return len(entries)
