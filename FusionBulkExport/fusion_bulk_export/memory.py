"""How much memory Fusion uses, how much the computer has left, and whether the computer is
under memory pressure. Standard library only.

sample() returns {"rss": bytes this process holds in physical memory now (not its peak),
"total": the computer's physical memory, "available": what can still be had without swapping,
"pressure_level": macOS's own memory pressure level (1 normal, 2 warning, 4 critical),
"commit_total" and "commit_available": Windows' commit limit and what is left of it}, each None
when it cannot be read (or does not exist on this system). Nothing here ever raises: memory
figures are for the report and the memory guard, and an export never fails for want of them.
Whether the figures mean "under pressure" is decided in core (system_pressure), next to the
guard's other numbers.

Windows: K32GetProcessMemoryInfo (kernel32; psapi's GetProcessMemoryInfo on old systems)
for the working set, GlobalMemoryStatusEx for total and available physical memory and for the
commit limit (ullTotalPageFile) and what is left of it (ullAvailPageFile). macOS: task_info with
MACH_TASK_BASIC_INFO for the resident size, sysctl hw.memsize for the total, host_statistics64
(free + inactive pages: free includes speculative pages) for available, sysctl
kern.memorystatus_vm_pressure_level for the pressure level. Linux (tests, scripts):
/proc/self/status and /proc/meminfo.
"""
import sys

GB = float(1 << 30)


def sample(platform=None):
    """{"rss", "total", "available", "pressure_level", "commit_total", "commit_available"}
    (bytes, but the level), each None when unknown. Never raises."""
    platform = sys.platform if platform is None else platform
    reader = {'win32': _windows, 'darwin': _macos}.get(platform, _linux if platform.startswith('linux') else None)
    result = dict((key, None) for key in KEYS)
    if reader is None:
        return result
    try:
        found = reader()
    except Exception:
        return result
    for key in result:
        try:
            value = found.get(key)
            # What is left of the commit limit may truly be 0; every other figure is positive.
            least = 0 if key == 'commit_available' else 1
            result[key] = int(value) if value is not None and int(value) >= least else None
        except Exception:
            result[key] = None
    return result


KEYS = ('rss', 'total', 'available', 'pressure_level', 'commit_total', 'commit_available')


def gigabytes(value):
    """Bytes as "1.4 GB" (one decimal), or None."""
    if value is None:
        return None
    return '%.1f GB' % (value / GB)


def _guard(produce):
    try:
        return produce()
    except Exception:
        return None


def _windows():
    import ctypes
    from ctypes import wintypes

    Counters, Status = windows_structures()
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)

    def rss():
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.GetCurrentProcess.argtypes = []
        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        function = getattr(kernel, 'K32GetProcessMemoryInfo', None)
        if function is None:
            function = ctypes.WinDLL('psapi', use_last_error=True).GetProcessMemoryInfo
        function.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        function.restype = wintypes.BOOL
        if not function(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return None
        return counters.WorkingSetSize

    def physical():
        status = Status()
        status.dwLength = ctypes.sizeof(Status)
        kernel.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(Status)]
        kernel.GlobalMemoryStatusEx.restype = wintypes.BOOL
        if not kernel.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return status.ullTotalPhys, status.ullAvailPhys, status.ullTotalPageFile, status.ullAvailPageFile

    found = _guard(physical) or (None, None, None, None)
    return dict(rss=_guard(rss), total=found[0], available=found[1], commit_total=found[2],
                commit_available=found[3])


def windows_structures():
    """PROCESS_MEMORY_COUNTERS and MEMORYSTATUSEX (DWORD as a fixed 32-bit field)."""
    import ctypes

    class Counters(ctypes.Structure):
        _fields_ = [('cb', ctypes.c_uint32), ('PageFaultCount', ctypes.c_uint32),
                    ('PeakWorkingSetSize', ctypes.c_size_t), ('WorkingSetSize', ctypes.c_size_t),
                    ('QuotaPeakPagedPoolUsage', ctypes.c_size_t), ('QuotaPagedPoolUsage', ctypes.c_size_t),
                    ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t), ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                    ('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t)]

    class Status(ctypes.Structure):
        _fields_ = [('dwLength', ctypes.c_uint32), ('dwMemoryLoad', ctypes.c_uint32),
                    ('ullTotalPhys', ctypes.c_uint64), ('ullAvailPhys', ctypes.c_uint64),
                    ('ullTotalPageFile', ctypes.c_uint64), ('ullAvailPageFile', ctypes.c_uint64),
                    ('ullTotalVirtual', ctypes.c_uint64), ('ullAvailVirtual', ctypes.c_uint64),
                    ('ullAvailExtendedVirtual', ctypes.c_uint64)]
    return Counters, Status


def mac_structures():
    """mach_task_basic_info (MACH_TASK_BASIC_INFO_COUNT 12) and vm_statistics64
    (HOST_VM_INFO64_COUNT 38), counted in 4-byte natural_t units."""
    import ctypes
    natural = ctypes.c_uint32

    class TimeValue(ctypes.Structure):
        _fields_ = [('seconds', ctypes.c_int32), ('microseconds', ctypes.c_int32)]

    # mach_task_basic_info is declared packed to 4 bytes; its fields fall on the same offsets
    # without packing (48 bytes either way), so no _pack_ is needed.
    class TaskInfo(ctypes.Structure):
        _fields_ = [('virtual_size', ctypes.c_uint64), ('resident_size', ctypes.c_uint64),
                    ('resident_size_max', ctypes.c_uint64), ('user_time', TimeValue),
                    ('system_time', TimeValue), ('policy', ctypes.c_int32), ('suspend_count', ctypes.c_int32)]

    class VmStatistics(ctypes.Structure):  # vm_statistics64
        _fields_ = [('free_count', natural), ('active_count', natural), ('inactive_count', natural),
                    ('wire_count', natural), ('zero_fill_count', ctypes.c_uint64),
                    ('reactivations', ctypes.c_uint64), ('pageins', ctypes.c_uint64),
                    ('pageouts', ctypes.c_uint64), ('faults', ctypes.c_uint64), ('cow_faults', ctypes.c_uint64),
                    ('lookups', ctypes.c_uint64), ('hits', ctypes.c_uint64), ('purges', ctypes.c_uint64),
                    ('purgeable_count', natural), ('speculative_count', natural),
                    ('decompressions', ctypes.c_uint64), ('compressions', ctypes.c_uint64),
                    ('swapins', ctypes.c_uint64), ('swapouts', ctypes.c_uint64),
                    ('compressor_page_count', natural), ('throttled_count', natural),
                    ('external_page_count', natural), ('internal_page_count', natural),
                    ('total_uncompressed_pages_in_compressor', ctypes.c_uint64)]
    return TaskInfo, VmStatistics


MACH_TASK_BASIC_INFO = 20
HOST_VM_INFO64 = 4


def _macos():
    import ctypes

    system = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
    natural = ctypes.c_uint32
    TaskInfo, VmStatistics = mac_structures()

    def task_self():
        try:
            return natural.in_dll(system, 'mach_task_self_').value
        except ValueError:
            system.mach_task_self.restype = natural
            return system.mach_task_self()

    def rss():
        info = TaskInfo()
        count = natural(ctypes.sizeof(TaskInfo) // 4)
        system.task_info.argtypes = [natural, ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(natural)]
        system.task_info.restype = ctypes.c_int
        if system.task_info(task_self(), MACH_TASK_BASIC_INFO, ctypes.byref(info), ctypes.byref(count)) != 0:
            return None
        return info.resident_size

    def sysctl(name, value):
        size = ctypes.c_size_t(ctypes.sizeof(value))
        system.sysctlbyname.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                                        ctypes.c_void_p, ctypes.c_size_t]
        system.sysctlbyname.restype = ctypes.c_int
        if system.sysctlbyname(name, ctypes.byref(value), ctypes.byref(size), None, 0) != 0:
            return None
        return value.value

    def total():
        return sysctl(b'hw.memsize', ctypes.c_uint64(0))

    def pressure_level():
        # The kernel's own judgment (what Activity Monitor's memory pressure graph shows):
        # 1 normal, 2 warning, 4 critical. An int; None where the name does not exist.
        return sysctl(b'kern.memorystatus_vm_pressure_level', ctypes.c_int32(0))

    def available():
        system.mach_host_self.restype = natural
        system.mach_host_self.argtypes = []
        host = system.mach_host_self()
        page = ctypes.c_size_t(0)  # vm_size_t: the kernel's page size, the unit of the counts
        system.host_page_size.argtypes = [natural, ctypes.POINTER(ctypes.c_size_t)]
        system.host_page_size.restype = ctypes.c_int
        if system.host_page_size(host, ctypes.byref(page)) != 0 or not page.value:
            return None
        stats = VmStatistics()
        count = natural(ctypes.sizeof(VmStatistics) // 4)
        system.host_statistics64.argtypes = [natural, ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(natural)]
        system.host_statistics64.restype = ctypes.c_int
        if system.host_statistics64(host, HOST_VM_INFO64, ctypes.byref(stats), ctypes.byref(count)) != 0:
            return None
        return (stats.free_count + stats.inactive_count) * page.value

    return dict(rss=_guard(rss), total=_guard(total), available=_guard(available),
                pressure_level=_guard(pressure_level))


def _linux():
    def field(path, name):
        with open(path, encoding='ascii', errors='replace') as stream:
            for line in stream:
                if line.startswith(name + ':'):
                    return int(line.split()[1]) * 1024  # kB
        return None
    return dict(rss=_guard(lambda: field('/proc/self/status', 'VmRSS')),
                total=_guard(lambda: field('/proc/meminfo', 'MemTotal')),
                available=_guard(lambda: field('/proc/meminfo', 'MemAvailable')))


SERIES_FIRST = 25   # the first point of run_info['memory']['series'] (and log line), so short runs have one...
SERIES_EVERY = 100  # ...then one every this many designs...
SERIES_MAX = 200    # ...and never more points: past this, every other one goes and the step doubles


class Tracker:
    """Fusion's memory over one run, kept in info (run_info['memory']): total and available
    physical memory, rss at the start, its peak and last value, and a bounded series of
    [designs, rss, available] at SERIES_FIRST designs, then every SERIES_EVERY designs. sampler() is sample() inside Fusion and
    a fake in tests; with none (the synthetic test API) nothing is recorded."""
    def __init__(self, sampler, info, log=None):
        self.sampler, self.info, self.log = sampler, info, log
        self.start = self.last = None

    @property
    def active(self):
        return self.sampler is not None

    def take(self):
        if self.sampler is None:
            return None
        try:
            found = self.sampler() or {}
            return dict((key, found.get(key)) for key in KEYS)
        except Exception:
            return None

    def begin(self):
        found = self.take()
        if found is None:
            return None
        self.start = self.last = found
        self.info.update(total=found['total'], start_rss=found['rss'], start_available=found['available'],
                         peak_rss=found['rss'], last_rss=found['rss'], last_available=found['available'],
                         samples=1, series_every=SERIES_EVERY, series=[[0, found['rss'], found['available']]])
        self.note('memory at start', found)
        return found

    def update(self, designs):
        """A sample after an item; designs: designs Fusion worked on so far in this run."""
        if self.start is None:
            return None
        found = self.take()
        if found is None:
            return None
        self.last = found
        info = self.info
        info['samples'] = info.get('samples', 0) + 1
        info.update(last_rss=found['rss'], last_available=found['available'])
        if found['total'] is not None:
            info['total'] = found['total']
        if found['rss'] is not None and (info.get('peak_rss') is None or found['rss'] > info['peak_rss']):
            info['peak_rss'] = found['rss']
        series = info['series']
        last = series[-1][0]
        due = SERIES_FIRST if last < SERIES_FIRST else (last // info['series_every'] + 1) * info['series_every']
        if designs >= due:
            series.append([designs, found['rss'], found['available']])
            self.note('memory after %d designs' % designs, found)
            if len(series) > SERIES_MAX:
                info['series'] = series[::2]
                info['series_every'] *= 2
        return found

    def finish(self):
        if self.start is not None and self.last is not None:
            self.note('memory at end', self.last)

    def note(self, what, found):
        if self.log is None:
            return
        megabytes = lambda value: 'unknown' if value is None else '%d MB' % (value >> 20)
        self.log('%s: Fusion %s, available %s of %s' % (what, megabytes(found['rss']), megabytes(found['available']),
                                                         megabytes(found['total'])))
