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
    """A fake memory.sample: rss grows by step per call; total and available as given."""
    def __init__(self, rss=1 * GB, step=10 << 20, total=16 * GB, available=8 * GB, fall=0):
        self.rss, self.step, self.total, self.available, self.fall = rss, step, total, available, fall
        self.calls = 0

    def __call__(self):
        self.calls += 1
        found = dict(rss=self.rss, total=self.total, available=self.available)
        self.rss += self.step
        self.available -= self.fall
        return found


class SamplingTests(unittest.TestCase):
    def test_sample_reads_this_process_and_the_machine_on_linux(self):
        found = memory.sample('linux')
        if not Path('/proc/meminfo').exists():
            self.skipTest('no /proc')
        self.assertGreater(found['rss'], 1 << 20)
        self.assertGreater(found['total'], found['available'])

    def test_sample_never_raises(self):
        self.assertEqual(memory.sample('plan9'), dict(rss=None, total=None, available=None))
        with patch.object(memory, '_windows', side_effect=OSError('no kernel32')):
            self.assertEqual(memory.sample('win32'), dict(rss=None, total=None, available=None))
        with patch.object(memory, '_macos', return_value=dict(rss=-1, total='x', available=5)):
            self.assertEqual(memory.sample('darwin'), dict(rss=None, total=None, available=5))
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
        self.assertEqual([point[0] for point in info['series']], list(range(0, 1001, 100)))
        self.assertEqual(info['samples'], 1001)
        self.assertTrue(all(line.startswith('memory') and 'MB' in line for line in lines))
        self.assertEqual(len(lines), 11)
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


class GuardRuleTests(unittest.TestCase):
    def test_available_memory_below_the_floor_stops(self):
        rule = core.memory_rule
        start = dict(rss=GB, total=16 * GB, available=8 * GB)
        self.assertIsNone(rule(start, dict(rss=2 * GB, total=16 * GB, available=int(1.6 * GB))))
        self.assertEqual(rule(start, dict(rss=2 * GB, total=16 * GB, available=int(1.4 * GB))), 'available')
        # 8% of a large machine is above 1.5 GB: 64 GB -> 5.12 GB.
        self.assertEqual(rule(start, dict(rss=2 * GB, total=64 * GB, available=5 * GB)), 'available')
        self.assertIsNone(rule(start, dict(rss=2 * GB, total=64 * GB, available=6 * GB)))
        # Total unknown: the 1.5 GB floor alone.
        self.assertEqual(rule(start, dict(rss=None, total=None, available=GB)), 'available')

    def test_growth_counts_only_when_fusion_holds_most_of_the_memory(self):
        rule = core.memory_rule
        start = dict(rss=GB, total=16 * GB, available=12 * GB)
        self.assertEqual(rule(start, dict(rss=10 * GB, total=16 * GB, available=4 * GB)), 'growth')
        self.assertIsNone(rule(start, dict(rss=9 * GB, total=16 * GB, available=4 * GB)))   # under 60%
        self.assertIsNone(rule(start, dict(rss=6 * GB, total=8 * GB, available=2 * GB)))    # grew 5 GB only
        self.assertIsNone(rule(dict(rss=None), dict(rss=12 * GB, total=16 * GB, available=None)))
        self.assertIsNone(rule(start, dict(rss=12 * GB, total=None, available=None)))

    def test_nothing_known_means_no_guard(self):
        self.assertIsNone(core.memory_rule(None, None))
        self.assertIsNone(core.memory_rule({}, dict(rss=None, total=None, available=None)))

    def test_the_test_limit_comes_from_the_environment_only_when_set(self):
        self.assertIsNone(core.memory_test_limit({}))
        self.assertIsNone(core.memory_test_limit({core.MEMORY_TEST_ENV: 'lots'}))
        self.assertIsNone(core.memory_test_limit({core.MEMORY_TEST_ENV: '0'}))
        self.assertEqual(core.memory_test_limit({core.MEMORY_TEST_ENV: '1500'}), 1500 << 20)
        self.assertEqual(core.memory_rule({}, dict(rss=1600 << 20), 1500 << 20), 'test')


class GuardRunTests(Base):
    def files(self, count=6):
        return [fake.File('Design %d' % n, 'design-%d' % n) for n in range(count)]

    def test_the_run_stops_cleanly_between_items_and_the_next_run_carries_on(self):
        files = self.files()
        released = []

        @contextlib.contextmanager
        def awake():
            try:
                yield
            finally:
                released.append(True)
        exporter = Exporter(self.root, self.adapter)
        # Available memory: 5 GB at the start, then 1 GB less after each item: 4, 3, 2, 1 (stop).
        exporter.memory_sampler = Sampler(total=16 * GB, available=5 * GB, fall=GB)
        with patch.object(core, 'keep_awake', awake):
            counts = exporter.run(fake.data(files))
        info = exporter.run_info
        self.assertEqual((info['stopped_code'], info['stopped_reason']), ('memory', 'Fusion is using a lot of memory'))
        self.assertEqual(info['memory_rule'], 'available')
        self.assertFalse(info['cancelled'])
        self.assertEqual(counts['exported'], 4)
        self.assertEqual(released, [True])
        self.assertFalse((self.root / 'export.lock').exists())
        self.assertFalse((self.root / 'manifest.journal').exists() and
                         (self.root / 'manifest.journal').read_text(encoding='utf-8').strip())
        saved = json.loads((self.root / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(sum(1 for e in saved['entries'] if e['status'] == 'done'), counts['exported'])
        self.assertEqual(saved['runs'][-1]['stopped_code'], 'memory')
        self.assertIn('stopped: memory (available rule)', (self.root / 'export.log').read_text(encoding='utf-8'))
        data = report.summary(counts, info, exporter.manifest, self.root)
        self.assertEqual(data['headline'], 'Stopped: Fusion is using a lot of memory')
        self.assertIn('Close Fusion, open it again and run the export on the same folder. '
                      'It carries on where it stopped.', data['notes'])
        page = report.render_html(exporter.manifest, info, counts)
        self.assertIn('The run stopped itself: Fusion is using a lot of memory. Close Fusion, open it again', page)
        # After restarting Fusion: memory is back, the run carries on and finishes.
        exporter = Exporter(self.root, self.adapter)
        exporter.memory_sampler = Sampler()
        later = exporter.run(fake.data(files))
        self.assertEqual(later['exported'] + counts['exported'], 6)
        self.assertIsNone(exporter.run_info['stopped_code'])

    def test_it_stops_after_the_item_that_crossed_the_line(self):
        exporter = Exporter(self.root, self.adapter)
        exporter.memory_sampler = Sampler(total=16 * GB, available=int(3.2 * GB), fall=GB)
        counts = exporter.run(fake.data(self.files()))
        # Samples: 3.2 at the start, then 2.2 (go on), 1.2 (stop) after the second design.
        self.assertEqual(counts['exported'], 2)

    def test_growth_rule_stops_a_run(self):
        exporter = Exporter(self.root, self.adapter)
        exporter.memory_sampler = Sampler(rss=GB, step=4 * GB, total=16 * GB, available=12 * GB)
        counts = exporter.run(fake.data(self.files()))
        # rss: 1 GB at the start, then 5, 9 (grew 8 GB but under 9.6 GB), 13: stop.
        self.assertEqual(counts['exported'], 3)
        self.assertEqual(exporter.run_info['memory_rule'], 'growth')

    def test_no_sampler_no_guard(self):
        exporter = Exporter(self.root, self.adapter)
        counts = exporter.run(fake.data(self.files()))
        self.assertEqual(counts['exported'], 6)
        self.assertIsNone(exporter.run_info['stopped_code'])

    def test_up_to_date_items_are_not_sampled(self):
        files = self.files(3)
        Exporter(self.root, self.adapter).run(fake.data(files))
        exporter = Exporter(self.root, self.adapter)
        exporter.memory_sampler = sampler = Sampler(available=GB)  # would stop at once
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


if __name__ == '__main__':
    unittest.main()
