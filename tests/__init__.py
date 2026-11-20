"""Make the installed add-in directory importable for synthetic tests."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'FusionBulkExport'))


class LinkedTemporaryDirectory:
    """A temporary folder reached through a symbolic link: .name is the link's path, never the
    real one. On macOS the temporary folder is /var/folders/... (a link to /private/var/...) and
    on Windows runners C:\\Users\\RUNNER~1\\... (a short name for C:\\Users\\runneradmin\\...), so
    Path.resolve() gives another spelling of the same folder; the add-in keeps the path it was
    given (a mapped drive keeps its letter). With a link here, a test that expects the resolved
    spelling fails on Linux too. Where links cannot be made, .name is the plain folder."""

    def __init__(self, prefix=None):
        import os
        import tempfile
        self._base = tempfile.TemporaryDirectory(prefix=prefix)
        real = Path(self._base.name) / 'real'
        real.mkdir()
        link = Path(self._base.name) / ((prefix or 'tmp') + 'link')
        try:
            os.symlink(str(real), str(link), target_is_directory=True)
        except (OSError, NotImplementedError, AttributeError):
            link = real
        self.name = str(link)
        self.linked = link != real

    def cleanup(self):
        import os
        if self.linked:  # the link first, so removing the folder never follows it
            try:
                (os.rmdir if os.name == 'nt' else os.unlink)(self.name)
            except OSError:
                pass
        self._base.cleanup()
