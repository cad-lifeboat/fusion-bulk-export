"""export.lock: one export at a time per output folder, also across Fusion processes.

The busy flag in the add-in covers one Fusion; two Fusion processes (or a second job into the
same folder) would otherwise clean each other's staged files mid-export and overwrite each
other's manifest. The lock file holds the process id, the computer's name and the start time.
It counts as stale (and is taken over) when its process is gone on this computer, when it is
older than STALE_SECONDS, or when it cannot be read.
"""
import json
import os
import socket
import sys
import time
import uuid

from .names import long_path

NAME = 'export.lock'
STALE_SECONDS = 12 * 3600
WRITING_SECONDS = 10  # an unreadable lock this young is being written (see FolderLock.create)
_HELD = set()  # lock files this process holds now


class FolderBusy(Exception):
    """Another export is using this output folder."""


def process_alive(pid):
    """Whether a process with this id runs on this computer. Never signals or stops it."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform == 'win32':
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return kernel32.GetLastError() == 5  # access denied: it exists
            try:
                code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return True
                return code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return True  # cannot tell: the age limit still applies
    try:
        os.kill(pid, 0)  # signal 0: only asks whether the process exists
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class FolderLock:
    def __init__(self, root, clock=time.time):
        self.path = os.path.join(str(root), NAME)
        self.clock = clock
        self.held = False

    def facts(self):
        try:
            with open(long_path(self.path), encoding='utf-8') as stream:
                value = json.loads(stream.read())
            return value if isinstance(value, dict) else None
        except (OSError, ValueError):
            return None

    def stale(self):
        facts = self.facts()
        if facts is None:
            # Unreadable or empty: stale, unless it was made a moment ago (an export creating it
            # where a lock cannot be written in one step; see acquire()).
            try:
                age = time.time() - os.stat(long_path(self.path)).st_mtime
            except OSError:
                return True
            return not (-WRITING_SECONDS < age < WRITING_SECONDS)
        started = facts.get('started')
        if not isinstance(started, (int, float)) or self.clock() - started > STALE_SECONDS:
            return True
        if facts.get('host') != socket.gethostname():
            return False  # another computer's export (a shared folder): only the age tells
        if facts.get('pid') == os.getpid():
            return self.path not in _HELD  # this process holds no such lock: left from before
        return not process_alive(facts.get('pid'))

    def acquire(self):
        """Take the lock, or raise FolderBusy."""
        data = json.dumps(dict(pid=os.getpid(), host=socket.gethostname(), started=self.clock())).encode('utf-8')
        for _ in range(2):
            try:
                self.create(data)
            except FileExistsError:
                if not self.stale():
                    raise FolderBusy('Another export is using this folder.')
                try:
                    os.unlink(long_path(self.path))
                except FileNotFoundError:
                    pass
                continue
            self.held = True
            _HELD.add(self.path)
            return
        raise FolderBusy('Another export is using this folder.')

    def create(self, data):
        """Create the lock with its contents in one step: written to a temporary file, then
        hard-linked to the lock's name, which fails (FileExistsError) when the lock exists, so
        no one ever sees it empty. Where hard links are not available (some network or FAT
        drives), an exclusive create then a write; stale() gives such a lock a moment."""
        temporary = '%s.%s.tmp' % (self.path, uuid.uuid4().hex[:12])
        try:
            with open(long_path(temporary), 'wb') as stream:
                stream.write(data)
            try:
                os.link(long_path(temporary), long_path(self.path))
                return
            except FileExistsError:
                raise
            except (OSError, NotImplementedError, AttributeError):
                pass  # no hard links here: the exclusive create below
        finally:
            try:
                os.unlink(long_path(temporary))
            except OSError:
                pass
        handle = os.open(long_path(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(handle, 'wb') as stream:
            stream.write(data)

    def release(self):
        if not self.held:
            return
        self.held = False
        _HELD.discard(self.path)
        try:
            os.unlink(long_path(self.path))
        except OSError:
            pass
