"""Long runs and memory: nothing of Fusion's is held past its design, the collector runs after
each item Fusion worked on."""
import errno
import gc
import json
import sys
from pathlib import Path
import tempfile
import unittest
import weakref
from types import SimpleNamespace as NS
from unittest.mock import patch

from tests import fake_adsk as fake
import contextlib
import importlib.util
import io

from fusion_bulk_export import core, errors, memory, report
from fusion_bulk_export.core import Exporter, Trouble

fake.install()
from fusion_bulk_export import fusion  # noqa: E402
from fusion_bulk_export.fusion import FusionAdapter  # noqa: E402


class Future:
    """A DataObjectFuture that can be watched with a weak reference."""
    def __init__(self):
        self.state = 1  # finished
        self.dataObject = NS(saveToFile=lambda path: Path(path).write_bytes(fake.PNG) and True)


class WatchedFile(fake.File):
    """A cloud file whose thumbnail is a fresh future on each read (as DataFile.thumbnail is)."""
    def __init__(self, *args, **kwargs):
        self.futures = []
        super().__init__(*args, **kwargs)

    @property
    def thumbnail(self):
        future = Future()
        self.futures.append(weakref.ref(future))
        return future

    @thumbnail.setter
    def thumbnail(self, value):
        pass


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fusion-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.app = NS(documents=fake.Documents())
        self.adapter = FusionAdapter(self.app)
        fusion.LEFT_OPEN.clear()
        self.addCleanup(fusion.LEFT_OPEN.clear)


class ReleaseTests(Base):
    def test_trouble_keeps_the_error_text_only(self):
        try:
            raise OSError(errno.ENOSPC, 'No space left on device')
        except OSError as error:
            trouble = Trouble('disk_full', 'The disk is full.', error)
        self.assertEqual(trouble.error, '[Errno %d] No space left on device' % errno.ENOSPC)
        self.assertIsNone(Trouble('failing', 'x').error)

    def test_a_failed_design_holds_no_document_or_thumbnail_when_the_next_item_starts(self):
        # Without the collector, so only references the add-in drops itself count.
        first, second = WatchedFile('A', 'first'), WatchedFile('B', 'second')
        execute = fake.Manager.execute

        def full_for_first(manager, path):
            if manager.file is first:
                raise OSError(errno.ENOSPC, 'No space left on device')
            return execute(manager, path)
        seen = []

        def progress(state):
            if state['phase'] == 'design' and state['name'] == 'B':
                documents = [weakref.ref(document) for document in first.documents]
                first.documents.clear()  # the synthetic file's own list of its documents
                seen.append([ref() is not None for ref in documents + first.futures])
        enabled = gc.isenabled()
        gc.disable()
        self.addCleanup(lambda: gc.enable() if enabled else None)
        with patch.object(fake.Manager, 'execute', full_for_first), patch.object(core, 'COLLECT_AFTER_ITEM', False):
            exporter = Exporter(self.root, self.adapter)
            exporter.run(fake.data([first, second]), progress=progress, on_trouble=lambda reason, last: 'retry')
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0])
        self.assertEqual(seen[0], [False] * len(seen[0]), 'a document or thumbnail of the failed design is still held')

    def test_the_collector_runs_after_each_item_fusion_worked_on(self):
        files = [fake.File('A', 'a'), fake.File('B', 'b')]
        with patch.object(core.gc, 'collect', return_value=0) as collect:
            exporter = Exporter(self.root, self.adapter)
            exporter.run(fake.data(files))
        self.assertEqual(collect.call_count, 2)
        self.assertEqual(exporter.run_info['gc']['collections'], 2)
        self.assertEqual(exporter.run_info['gc']['full'], 0)
        self.assertIn('seconds', exporter.run_info['gc'])
        with patch.object(core.gc, 'collect', return_value=0) as collect:  # up to date: Fusion not asked
            exporter = Exporter(self.root, self.adapter)
            exporter.run(fake.data(files))
        self.assertEqual(collect.call_count, 0)
        with patch.object(core.gc, 'collect', return_value=0) as collect, patch.object(core, 'COLLECT_AFTER_ITEM', False):
            exporter = Exporter(self.root, self.adapter)
            exporter.run(fake.data([fake.File('C', 'c')]))
        self.assertEqual(collect.call_count, 0)
        self.assertNotIn('gc', exporter.run_info)

    def test_every_fiftieth_collection_is_a_full_one(self):
        exporter = Exporter(self.root, self.adapter)
        with patch.object(core.gc, 'collect', return_value=1) as collect:
            for _ in range(100):
                exporter.collect()
        generations = [call.args for call in collect.call_args_list]
        self.assertEqual(generations.count(()), 2)
        self.assertEqual(generations.count((1,)), 98)
        self.assertEqual(exporter.run_info['gc']['full'], 2)
        self.assertEqual(exporter.run_info['gc']['objects'], 100)


GB = 1 << 30


class Sampler:
    """A fake memory.sample: rss grows by step per call; the other figures as given."""
    def __init__(self, rss=1 * GB, step=10 << 20, total=16 * GB, available=8 * GB, fall=0, **more):
        self.rss, self.step, self.total, self.available, self.fall = rss, step, total, available, fall
        self.more = more  # pressure_level, commit_total, commit_available
        self.calls = 0

    def __call__(self):
        self.calls += 1
        found = dict(rss=self.rss, total=self.total, available=self.available, **self.more)
        self.rss += self.step
        self.available -= self.fall
        return found


class Clock:
    """A fake time.monotonic for the guard: each reading is step seconds after the last."""
    def __init__(self, step=10.0):
        self.now, self.step = 0.0, step

    def __call__(self):
        self.now += self.step
        return self.now


class SamplingTests(unittest.TestCase):
    def test_sample_reads_this_process_and_the_machine_on_linux(self):
        found = memory.sample('linux')
        if not Path('/proc/meminfo').exists():
            self.skipTest('no /proc')
        self.assertGreater(found['rss'], 1 << 20)
        self.assertGreater(found['total'], found['available'])

    def test_sample_never_raises(self):
        nothing = dict(rss=None, total=None, available=None, pressure_level=None, commit_total=None,
                       commit_available=None)
        self.assertEqual(memory.sample('plan9'), nothing)
        with patch.object(memory, '_windows', side_effect=OSError('no kernel32')):
            self.assertEqual(memory.sample('win32'), nothing)
        with patch.object(memory, '_macos', return_value=dict(rss=-1, total='x', available=5, pressure_level=2)):
            self.assertEqual(memory.sample('darwin'), dict(nothing, available=5, pressure_level=2))
        # Windows: no commit left is a real figure (0), not an unknown one.
        with patch.object(memory, '_windows', return_value=dict(rss=5, commit_total=9, commit_available=0)):
            self.assertEqual(memory.sample('win32'), dict(nothing, rss=5, commit_total=9, commit_available=0))
        # The real calls on another system fail inside and give None, never an error.
        self.assertIn(memory.sample('win32' if sys.platform != 'win32' else 'darwin')['rss'], (None,))

    def test_structures_match_the_system_headers(self):
        import ctypes
        task, vm = memory.mac_structures()
        self.assertEqual(ctypes.sizeof(task) // 4, 12)       # MACH_TASK_BASIC_INFO_COUNT
        self.assertEqual(task.resident_size.offset, 8)
        self.assertEqual(ctypes.sizeof(vm) // 4, 38)         # HOST_VM_INFO64_COUNT
        self.assertEqual(vm.inactive_count.offset, 8)
        counters, status = memory.windows_structures()
        if ctypes.sizeof(ctypes.c_size_t) == 8:
            self.assertEqual(ctypes.sizeof(counters), 72)    # PROCESS_MEMORY_COUNTERS, 64-bit
            self.assertEqual(counters.WorkingSetSize.offset, 16)
        self.assertEqual(ctypes.sizeof(status), 64)          # MEMORYSTATUSEX

    def test_tracker_keeps_start_peak_last_and_a_bounded_series(self):
        info, lines = {}, []
        tracker = memory.Tracker(Sampler(), info, lines.append)
        tracker.begin()
        for designs in range(1, 1001):
            tracker.update(designs)
        self.assertEqual(info['start_rss'], GB)
        self.assertEqual(info['last_rss'], GB + 1000 * (10 << 20))
        self.assertEqual(info['peak_rss'], info['last_rss'])
        # A point (and a log line) at design 25, so short runs have one, then every 100.
        self.assertEqual([point[0] for point in info['series']], [0, 25] + list(range(100, 1001, 100)))
        self.assertEqual(info['samples'], 1001)
        self.assertTrue(all(line.startswith('memory') and 'MB' in line for line in lines))
        self.assertEqual(len(lines), 12)
        self.assertTrue(lines[1].startswith('memory after 25 designs: Fusion'))
        with patch.object(memory, 'SERIES_MAX', 5):
            info = {}
            tracker = memory.Tracker(Sampler(), info)
            tracker.begin()
            for designs in range(1, 3001):
                tracker.update(designs)
            self.assertLessEqual(len(info['series']), 6)
            self.assertGreater(info['series_every'], 100)

    def test_no_sampler_or_a_failing_one_records_nothing(self):
        info = {}
        self.assertIsNone(memory.Tracker(None, info).begin())
        tracker = memory.Tracker(lambda: 1 / 0, info)
        self.assertIsNone(tracker.begin())
        self.assertIsNone(tracker.update(5))
        self.assertEqual(info, {})


class RunMemoryTests(Base):
    def test_the_run_records_memory_and_the_report_shows_it(self):
        exporter = Exporter(self.root, self.adapter)
        exporter.memory_sampler = Sampler()
        exporter.run(fake.data([fake.File('A', 'a'), fake.File('B', 'b'), fake.File('Up', 'up', extension='pdf')]))
        facts = exporter.run_info['memory']
        self.assertEqual(facts['samples'], 4)  # start and after each of the three items
        self.assertEqual(facts['start_rss'], GB)
        self.assertEqual(facts['total'], 16 * GB)
        saved = json.loads((self.root / 'manifest.json').read_text(encoding='utf-8'))['runs'][-1]['memory']
        self.assertEqual(saved['last_rss'], facts['last_rss'])
        log = (self.root / 'export.log').read_text(encoding='utf-8')
        self.assertIn('memory at start: Fusion 1024 MB, available 8192 MB of 16384 MB', log)
        self.assertIn('memory at end', log)
        page = report.render_html(exporter.manifest, exporter.run_info, exporter.run_info['counts'])
        self.assertIn('Fusion memory: 1.0 GB (1.0 GB at its start).', page)

    def test_without_a_sampler_nothing_is_recorded_or_shown(self):
        exporter = Exporter(self.root, self.adapter)
        self.assertIsNone(exporter.memory_sampler)  # the synthetic API: not inside Fusion
        exporter.run(fake.data([fake.File('A', 'a')]))
        self.assertNotIn('memory', exporter.run_info)
        self.assertNotIn('Fusion memory', report.render_html(exporter.manifest, exporter.run_info, {}))

    def test_memory_line_wording(self):
        self.assertIsNone(report.memory_line({}))
        line = report.memory_line(dict(memory=dict(last_rss=int(1.4 * GB), peak_rss=int(1.6 * GB), start_rss=int(0.8 * GB))))
        self.assertEqual(line, 'Fusion memory: 1.4 GB (at most 1.6 GB during the last run, 0.8 GB at its start).')


MAC_8GB = 8 * GB
WIN_32GB = 32 * GB


def guard_run(samples, start=None, step=10.0, enabled=True):
    """Feed samples to a MemoryGuard, one check each step seconds apart: (the check number that
    stopped, or None; the guard)."""
    guard = core.MemoryGuard(enabled, Clock(step))
    start = start or samples[0]
    for number, sample in enumerate(samples, 1):
        if guard.check(start, sample):
            return number, guard
    return None, guard


class GuardRuleTests(unittest.TestCase):
    """The three cases measured on real computers, then the rules around them."""

    def test_measured_8gb_mac_with_an_idle_fusion_never_stops(self):
        # Fusion 0.7-1.1 GB, available (free + inactive) 1.4-2.1 GB, dipping while a design opens.
        samples = []
        for n in range(300):
            rss = int((0.7 + 0.4 * (n % 5) / 4) * GB)
            available = int((1.4 if n % 3 == 0 else 2.1) * GB)
            samples.append(dict(rss=rss, total=MAC_8GB, available=available, pressure_level=1))
        self.assertEqual(guard_run(samples)[0], None)
        # Even if macOS said "warning" or "critical" throughout: Fusion is small, a restart cannot help.
        # Only a lasting "critical" gets the low-memory note; "warning", which an 8 GB Mac with
        # Fusion open reports almost all the time, does not.
        for level, note in ((2, False), (4, True)):
            stopped, guard = guard_run([dict(sample, pressure_level=level) for sample in samples])
            self.assertIsNone(stopped)
            self.assertIs(guard.low_memory, note)
        for sample in samples:
            self.assertIsNone(core.memory_condition(samples[0], sample))

    def test_measured_32gb_windows_with_a_small_fusion_and_busy_other_programs_never_stops(self):
        # Fusion 1.4 GB; other programs hold the memory: 1.35 GB available. Commit short too
        # (the worst case): the computer is under pressure, but restarting Fusion would not help.
        sample = dict(rss=int(1.4 * GB), total=WIN_32GB, available=int(1.35 * GB), commit_total=40 * GB,
                      commit_available=int(0.5 * GB))
        self.assertTrue(core.system_pressure(sample))
        self.assertIsNone(core.memory_condition(dict(sample, rss=int(1.3 * GB)), sample))
        stopped, guard = guard_run([sample] * 200, start=dict(sample, rss=int(1.3 * GB)))
        self.assertIsNone(stopped)
        self.assertTrue(guard.low_memory)

    def test_measured_32gb_windows_with_fusion_at_10_9_gb_may_stop_when_it_lasts(self):
        # Fusion grew from 1.46 GB to 10.9 GB, 1.5 GB available, Windows short of commit.
        start = dict(rss=int(1.458 * GB), total=WIN_32GB, available=int(4.4 * GB), commit_total=40 * GB,
                     commit_available=12 * GB)
        high = dict(rss=int(10.9 * GB), total=WIN_32GB, available=int(1.5 * GB), commit_total=40 * GB,
                    commit_available=int(1.2 * GB))
        self.assertEqual(core.memory_condition(start, high), 'growth')
        calm = [dict(start, rss=int((1.5 + 0.3 * n) * GB)) for n in range(30)]
        stopped, guard = guard_run(calm + [high] * 10, start=start)
        self.assertEqual(stopped, 30 + core.MEMORY_SUSTAIN_CHECKS)
        self.assertFalse(guard.low_memory)  # Fusion is the cause: no "close other programs" note
        # Plenty of commit left: Windows can page it out, nothing is about to fail. No stop.
        roomy = dict(high, commit_available=20 * GB)
        self.assertIsNone(core.memory_condition(start, roomy))
        self.assertIsNone(guard_run(calm + [roomy] * 50, start=start)[0])

    def test_one_low_sample_never_stops_only_a_lasting_one(self):
        start = dict(rss=GB, total=16 * GB)
        big = dict(rss=13 * GB, total=16 * GB)   # over 70% of 16 GB
        small = dict(rss=GB, total=16 * GB)
        # Every fifth check high, for a long time: never five in a row.
        self.assertIsNone(guard_run([big if n % 5 == 0 else small for n in range(400)], start)[0])
        # Five in a row, but within 8 s (2 s apart): not yet lasting. Then it lasts 30 s: stop.
        samples = [small] * 30 + [big] * 20
        stopped, _ = guard_run(samples, start, step=2.0)
        self.assertEqual(stopped, 30 + 16)  # checks 31..46 span 30 s at 2 s apart
        stopped, _ = guard_run(samples, start, step=10.0)
        self.assertEqual(stopped, 30 + core.MEMORY_SUSTAIN_CHECKS)

    def test_never_in_the_first_25_items(self):
        big = dict(rss=7 * GB, total=MAC_8GB)  # Fusion alone holds 7 of 8 GB from the first item
        stopped, _ = guard_run([big] * 100, dict(rss=GB, total=MAC_8GB), step=60.0)
        self.assertEqual(stopped, core.MEMORY_MIN_ITEMS)
        self.assertEqual(core.MEMORY_MIN_ITEMS, 25)

    def test_fusion_itself_over_70_percent_stops_without_pressure(self):
        start = dict(rss=GB, total=MAC_8GB)
        self.assertEqual(core.memory_condition(start, dict(rss=6 * GB, total=MAC_8GB, pressure_level=1)), 'fusion')
        self.assertIsNone(core.memory_condition(start, dict(rss=5 * GB, total=MAC_8GB, pressure_level=1)))
        # Grown by over 4 GB: only with pressure.
        self.assertEqual(core.memory_condition(start, dict(rss=int(5.5 * GB), total=MAC_8GB, pressure_level=2)),
                         'growth')
        self.assertIsNone(core.memory_condition(start, dict(rss=int(4.9 * GB), total=MAC_8GB, pressure_level=4)))

    def test_a_small_fusion_never_stops_even_on_a_small_computer(self):
        tiny = 2 * GB
        sample = dict(rss=int(1.9 * GB), total=tiny, pressure_level=4)
        self.assertIsNone(core.memory_condition(dict(rss=GB), sample))

    def test_pressure_signals(self):
        pressure = core.system_pressure
        self.assertFalse(pressure(dict(pressure_level=1)))
        self.assertTrue(pressure(dict(pressure_level=2)))
        self.assertTrue(pressure(dict(pressure_level=4)))
        # macOS free + inactive is not a pressure signal: only the kernel's level counts.
        self.assertIsNone(pressure(dict(total=MAC_8GB, available=100 << 20)))
        win = dict(total=WIN_32GB, available=GB, commit_total=40 * GB)
        self.assertTrue(pressure(dict(win, commit_available=int(1.9 * GB))))     # under 5% of 40 GB
        self.assertFalse(pressure(dict(win, commit_available=int(2.1 * GB))))
        self.assertFalse(pressure(dict(win, available=4 * GB, commit_available=GB // 2)))  # RAM left
        self.assertTrue(pressure(dict(total=WIN_32GB, available=GB, commit_total=10 * GB,
                                      commit_available=900 << 20)))             # under 1 GB
        self.assertIsNone(pressure({}))
        self.assertIsNone(pressure(None))

    def test_nothing_known_means_no_guard(self):
        self.assertIsNone(core.memory_condition(None, None))
        self.assertIsNone(core.memory_condition({}, dict(rss=None, total=None, available=None)))
        self.assertIsNone(guard_run([dict(rss=None)] * 100)[0])

    def test_the_kill_switch(self):
        enabled = core.memory_guard_enabled
        self.assertTrue(enabled({}, {}))
        for value in ('off', 'OFF', '0', 'false', 'no'):
            self.assertFalse(enabled({core.MEMORY_GUARD_ENV: value}, {}))
        self.assertTrue(enabled({core.MEMORY_GUARD_ENV: 'on'}, {}))
        self.assertFalse(enabled({}, dict(memory_guard=False)))
        self.assertTrue(enabled({}, dict(memory_guard=True)))
        with tempfile.TemporaryDirectory() as folder:
            stored = Path(folder) / 'settings.json'
            stored.write_text(json.dumps(dict(memory_guard=False)), encoding='utf-8')
            with patch.dict('os.environ', {'FUSION_BULK_EXPORT_SETTINGS': str(stored)}):
                self.assertFalse(enabled({}))
        big = dict(rss=7 * GB, total=MAC_8GB)
        self.assertIsNone(guard_run([big] * 100, enabled=False)[0])

    def test_the_test_limit_comes_from_the_environment_only_when_set(self):
        self.assertIsNone(core.memory_test_limit({}))
        self.assertIsNone(core.memory_test_limit({core.MEMORY_TEST_ENV: 'lots'}))
        self.assertIsNone(core.memory_test_limit({core.MEMORY_TEST_ENV: '0'}))
        self.assertEqual(core.memory_test_limit({core.MEMORY_TEST_ENV: '1500'}), 1500 << 20)
        guard = core.MemoryGuard(True, Clock(), 1500 << 20)
        self.assertEqual(guard.check({}, dict(rss=1600 << 20)), 'test')  # at once: it is for testing


class GuardRunTests(Base):
    def files(self, count=30):
        return [fake.File('Design %d' % n, 'design-%d' % n) for n in range(count)]

    def exporter(self, sampler):
        exporter = Exporter(self.root, self.adapter)
        exporter.memory_sampler = sampler
        exporter.memory_clock = Clock(10.0)
        return exporter

    def test_the_run_stops_cleanly_between_items_and_the_next_run_carries_on(self):
        files = self.files()
        released = []

        @contextlib.contextmanager
        def awake():
            try:
                yield
            finally:
                released.append(True)
        # Fusion holds 6 of 8 GB (over 70%) from the first item: the guard waits for item 25.
        exporter = self.exporter(Sampler(rss=6 * GB, step=0, total=MAC_8GB, available=GB))
        with patch.object(core, 'keep_awake', awake):
            counts = exporter.run(fake.data(files))
        info = exporter.run_info
        self.assertEqual((info['stopped_code'], info['stopped_reason']), ('memory', 'Fusion is using a lot of memory'))
        self.assertEqual(info['memory_rule'], 'fusion')
        self.assertFalse(info['cancelled'])
        self.assertEqual(counts['exported'], 25)
        self.assertEqual(released, [True])
        self.assertFalse((self.root / 'export.lock').exists())
        self.assertFalse((self.root / 'manifest.journal').exists() and
                         (self.root / 'manifest.journal').read_text(encoding='utf-8').strip())
        saved = json.loads((self.root / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(sum(1 for e in saved['entries'] if e['status'] == 'done'), counts['exported'])
        self.assertEqual(saved['runs'][-1]['stopped_code'], 'memory')
        self.assertIn('stopped: memory (fusion rule, 25 checks in a row)',
                      (self.root / 'export.log').read_text(encoding='utf-8'))
        data = report.summary(counts, info, exporter.manifest, self.root)
        self.assertEqual(data['headline'], 'Stopped: Fusion is using a lot of memory')
        self.assertIn('Close Fusion, open it again and run the export on the same folder. '
                      'It carries on where it stopped.', data['notes'])
        self.assertNotIn(core.LOW_MEMORY_NOTE, data['notes'])
        page = report.render_html(exporter.manifest, info, counts)
        self.assertIn('The run stopped itself: Fusion is using a lot of memory. Close Fusion, open it again', page)
        # After restarting Fusion: memory is back, the run carries on and finishes.
        exporter = self.exporter(Sampler())
        later = exporter.run(fake.data(files))
        self.assertEqual(later['exported'] + counts['exported'], 30)
        self.assertIsNone(exporter.run_info['stopped_code'])

    def test_measured_8gb_mac_exports_everything(self):
        # The 8 GB Mac's figures that stopped every launch of the earlier guard: now no stop.
        exporter = self.exporter(Sampler(rss=int(0.7 * GB), step=5 << 20, total=MAC_8GB, available=int(1.45 * GB),
                                         pressure_level=1))
        counts = exporter.run(fake.data(self.files(59)))
        self.assertEqual(counts['exported'], 59)
        self.assertIsNone(exporter.run_info['stopped_code'])
        self.assertNotIn('low_memory', exporter.run_info)

    def test_other_programs_using_the_memory_get_a_note_not_a_stop(self):
        exporter = self.exporter(Sampler(rss=int(1.4 * GB), step=0, total=WIN_32GB, available=int(1.35 * GB),
                                         commit_total=40 * GB, commit_available=GB // 2))
        counts = exporter.run(fake.data(self.files(40)))
        info = exporter.run_info
        self.assertEqual(counts['exported'], 40)
        self.assertIsNone(info['stopped_code'])
        self.assertTrue(info['low_memory'])
        data = report.summary(counts, info, exporter.manifest, self.root)
        self.assertIn('This computer was low on memory during the export; closing other programs may speed it up.',
                      data['notes'])
        self.assertIn(core.LOW_MEMORY_NOTE, report.render_html(exporter.manifest, info, counts))

    def test_macos_warning_level_alone_gives_no_note(self):
        exporter = self.exporter(Sampler(rss=int(0.9 * GB), step=0, total=MAC_8GB, available=int(1.5 * GB),
                                         pressure_level=2))
        counts = exporter.run(fake.data(self.files(40)))
        self.assertEqual(counts['exported'], 40)
        self.assertNotIn('low_memory', exporter.run_info)
        self.assertNotIn(core.LOW_MEMORY_NOTE, report.summary(counts, exporter.run_info, exporter.manifest,
                                                              self.root)['notes'])

    def test_macos_critical_level_that_lasts_gives_the_note(self):
        exporter = self.exporter(Sampler(rss=int(0.9 * GB), step=0, total=MAC_8GB, available=int(0.3 * GB),
                                         pressure_level=4))
        exporter.run(fake.data(self.files(40)))
        self.assertTrue(exporter.run_info['low_memory'])

    def test_critical_pressure_levels(self):
        self.assertFalse(core.critical_pressure(dict(pressure_level=1)))
        self.assertFalse(core.critical_pressure(dict(pressure_level=2)))
        self.assertTrue(core.critical_pressure(dict(pressure_level=4)))
        self.assertIsNone(core.critical_pressure({}))
        windows = dict(total=WIN_32GB, available=int(1.35 * GB), commit_total=40 * GB, commit_available=GB // 2)
        self.assertTrue(core.critical_pressure(windows))
        self.assertFalse(core.critical_pressure(dict(windows, commit_available=20 * GB)))
        # The stop rule's pressure is unchanged: warning still counts there.
        self.assertTrue(core.system_pressure(dict(pressure_level=2)))

    def test_growth_with_pressure_stops_a_run(self):
        # 1 GB at the start, +0.25 GB per item; macOS says "warning" all along. Grown over 4 GB
        # from item 17 (5.25 GB; 7 GB at item 24, still under 70% of 16 GB), so the rule holds
        # from there; the guard waits for item 25.
        exporter = self.exporter(Sampler(rss=GB, step=GB // 4, total=16 * GB, available=GB, pressure_level=2))
        counts = exporter.run(fake.data(self.files()))
        self.assertEqual(counts['exported'], 25)
        self.assertEqual(exporter.run_info['memory_rule'], 'growth')

    def test_switched_off_it_never_stops(self):
        exporter = self.exporter(Sampler(rss=7 * GB, step=0, total=MAC_8GB))
        with patch.dict('os.environ', {core.MEMORY_GUARD_ENV: 'off'}):
            counts = exporter.run(fake.data(self.files()))
        self.assertEqual(counts['exported'], 30)
        self.assertEqual(exporter.run_info['memory_guard'], 'off')
        self.assertIn('memory guard: off', (self.root / 'export.log').read_text(encoding='utf-8'))
        exporter = self.exporter(Sampler(rss=7 * GB, step=0, total=MAC_8GB))
        exporter.memory_guard = False  # a job's "memory_guard": false
        exporter.run(fake.data(self.files(31)))
        self.assertIsNone(exporter.run_info['stopped_code'])

    def test_no_sampler_no_guard(self):
        exporter = Exporter(self.root, self.adapter)
        counts = exporter.run(fake.data(self.files()))
        self.assertEqual(counts['exported'], 30)
        self.assertIsNone(exporter.run_info['stopped_code'])

    def test_up_to_date_items_are_not_sampled(self):
        files = self.files(3)
        Exporter(self.root, self.adapter).run(fake.data(files))
        exporter = self.exporter(Sampler(rss=7 * GB, total=MAC_8GB))
        sampler = exporter.memory_sampler
        counts = exporter.run(fake.data(files))
        self.assertEqual((counts['up_to_date'], sampler.calls), (3, 1))
        self.assertIsNone(exporter.run_info['stopped_code'])

    def test_the_stop_text_is_a_plain_user_error(self):
        known = errors.friendly(Trouble('memory', core.MEMORY_REASON))
        self.assertEqual(known.text(), 'The export stopped: Fusion is using a lot of memory\n\n'
                                       'Close Fusion, open it again and run the export on the same folder. '
                                       'It carries on where it stopped.')


spec = importlib.util.spec_from_file_location('autorun_launcher_memory', Path(__file__).parent.parent / 'scripts' / 'autorun.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class LauncherMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_a_memory_stop_exits_with_its_own_status_and_one_plain_line(self):
        out, installed = self.root / 'out', self.root / 'inst'
        out.mkdir()
        installed.mkdir()
        (out / 'autorun-result.json').write_text(json.dumps(dict(
            mode='export', counts=dict(exported=120), stopped_code='memory',
            stopped_reason='Fusion is using a lot of memory', errors=[])))
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            status = launcher.wait(installed, out, 100, sleep=lambda _: None, clock=lambda: 0)
        self.assertEqual(status, launcher.EXIT_MEMORY)
        self.assertEqual(launcher.EXIT_MEMORY, 75)
        self.assertEqual(stream.getvalue().count(launcher.MEMORY_LINE), 1)
        self.assertIn('Stopped by itself (memory): Fusion is using a lot of memory.', stream.getvalue())
        self.assertIn('75 the export stopped itself because', launcher.__doc__.replace('\n', ' '))


class SwitchTests(unittest.TestCase):
    """The kill switch reaches the add-in from the launcher's environment, through the job."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_the_launcher_passes_the_switch_in_the_job(self):
        installed, out = self.root / 'AddIns' / 'FusionBulkExport', self.root / 'out'
        installed.mkdir(parents=True)
        with patch.dict('os.environ', {launcher.MEMORY_GUARD_ENV: 'off'}), \
                patch.object(launcher, 'update_addin', lambda installed: False):
            launcher.install(installed, out, 'export')
        self.assertIs(json.loads((installed / 'autorun.json').read_text(encoding='utf-8'))['memory_guard'], False)
        with patch.dict('os.environ', {launcher.MEMORY_GUARD_ENV: 'on'}), \
                patch.object(launcher, 'update_addin', lambda installed: False):
            launcher.install(installed, out, 'export')
        self.assertNotIn('memory_guard', json.loads((installed / 'autorun.json').read_text(encoding='utf-8')))
        self.assertEqual(launcher.MEMORY_GUARD_ENV, core.MEMORY_GUARD_ENV)
        self.assertEqual(launcher.LOW_MEMORY_NOTE, core.LOW_MEMORY_NOTE)

    def test_the_add_in_reads_the_switch_from_the_job(self):
        from fusion_bulk_export import autorun
        self.assertIs(autorun.check_settings(dict(memory_guard=False))[-1], False)
        self.assertIs(autorun.check_settings({})[-1], True)
        with self.assertRaises(ValueError):
            autorun.check_settings(dict(memory_guard='off'))

    def test_the_launcher_says_when_the_computer_was_low_on_memory(self):
        out = self.root / 'out'
        out.mkdir()
        (out / 'autorun-result.json').write_text(json.dumps(dict(mode='export', counts=dict(exported=3),
                                                                 low_memory=True)))
        self.assertIn('Note: ' + core.LOW_MEMORY_NOTE, launcher.summary(out))
        (out / 'autorun-result.json').write_text(json.dumps(dict(mode='export', counts=dict(exported=3))))
        self.assertFalse([line for line in launcher.summary(out) if 'low on memory' in line])


if __name__ == '__main__':
    unittest.main()
