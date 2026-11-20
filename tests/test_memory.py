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
from fusion_bulk_export import core, memory, report
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


if __name__ == '__main__':
    unittest.main()
