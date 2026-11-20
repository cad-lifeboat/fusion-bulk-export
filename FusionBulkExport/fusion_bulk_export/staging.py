"""Short paths for Fusion to write to.

Fusion's exporter hangs (its main thread stops, with no dialog) when handed a path over 260
characters on Windows, even with the "\\\\?\\" prefix. So Fusion is never given an item's final
path: it writes to a short file in a staging folder, and this add-in then moves the file to its
readable final path, which may be as long as the file system allows (names.long_path).

The staging folder is ".staging" at the output root, or a folder in the system's temporary folder
when the output folder is itself so deep that even that would be long, or is a Windows network
path (\\\\server\\share\\...: Fusion is only ever handed local short paths; a mapped drive letter
such as X: is used like any drive). From the temporary folder a file is copied to the output
folder under a temporary name and then renamed, so a final file is never half-written. The
staging folder is emptied when a run starts and when it ends.
"""
import errno
import hashlib
import os
from pathlib import Path
import shutil
import sys
import tempfile
import uuid

from .names import is_unc, long_path

NAME = '.staging'
MAX_FOLDER = 200    # characters in the staging folder's path; a staged file adds about 20
FUSION_LIMIT = 259  # Fusion is never given a longer path (nor a "\\?\" one)


class Staging:
    def __init__(self, root, temporary=None, windows=None):
        windows = sys.platform == 'win32' if windows is None else windows
        root = Path(root)
        folder = root / NAME
        if len(str(folder)) > MAX_FOLDER or (windows and is_unc(root)):
            base = Path(temporary or tempfile.gettempdir())
            folder = base / ('FusionBulkExport-' + hashlib.sha256(str(root).encode('utf-8')).hexdigest()[:10])
        self.folder = folder

    def path(self, suffix=''):
        """A new short file path in the staging folder (the folder is created; the file is not)."""
        os.makedirs(long_path(self.folder), exist_ok=True)
        path = str(self.folder / (uuid.uuid4().hex[:12] + suffix))
        if len(path) > FUSION_LIMIT:
            raise ValueError('No short staging path is available (%d characters)' % len(path))
        return path

    def fresh_folder(self):
        """A new empty folder in the staging folder."""
        path = self.path()
        os.makedirs(long_path(path))
        return path

    def place(self, staged, final):
        """Move a staged file to its final path (replacing a file there)."""
        try:
            os.replace(long_path(staged), long_path(final))
        except OSError as error:
            if error.errno != errno.EXDEV and getattr(error, 'winerror', None) != 17:
                raise
            # The temporary folder is on another disk or a network share: copy beside the final
            # file, rename it into place, then remove the staged one.
            partial = str(final) + '.partial'
            try:
                shutil.copyfile(long_path(staged), long_path(partial))
                os.replace(long_path(partial), long_path(final))
            except BaseException:
                try:
                    os.unlink(long_path(partial))
                except OSError:
                    pass
                raise
            os.unlink(long_path(staged))
        return str(final)

    def clean(self):
        """Remove whatever a run left in the staging folder. Never raises."""
        shutil.rmtree(long_path(self.folder), ignore_errors=True)
