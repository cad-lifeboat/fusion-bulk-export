"""Keep the computer awake while an export runs.

macOS: a `caffeinate -i -w <pid>` child holds off idle sleep and ends by itself if Fusion
exits. Windows: SetThreadExecutionState on the exporting thread, cleared at the end.
Anywhere else, or under the synthetic test API, or if anything fails, it does nothing:
staying awake is a convenience and never stops an export.
"""
import contextlib
import os
import subprocess
import sys

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def inside_fusion():
    """True only when the real Fusion API is loaded (not in scripts, not under the test API)."""
    adsk = sys.modules.get('adsk')
    return adsk is not None and not getattr(adsk, '_synthetic', False)


@contextlib.contextmanager
def keep_awake(platform=None, active=None):
    """Context manager. platform defaults to sys.platform; active defaults to inside_fusion()."""
    platform = sys.platform if platform is None else platform
    if not (inside_fusion() if active is None else active):
        yield
        return
    release = None
    try:
        if platform == 'darwin':
            child = subprocess.Popen(['caffeinate', '-i', '-w', str(os.getpid())],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)

            def release():
                child.terminate()
                child.wait(5)
        elif platform == 'win32':
            import ctypes
            kernel = ctypes.windll.kernel32
            if kernel.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED):

                def release():
                    kernel.SetThreadExecutionState(ES_CONTINUOUS)
    except Exception:
        release = None
    try:
        yield
    finally:
        if release is not None:
            try:
                release()
            except Exception:
                pass
