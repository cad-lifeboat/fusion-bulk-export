"""Long runs and memory: nothing of Fusion's is held past its design, the collector runs after
each item Fusion worked on."""
import errno
import gc
import json
from pathlib import Path
import tempfile
import unittest
import weakref
from types import SimpleNamespace as NS
from unittest.mock import patch

from tests import fake_adsk as fake
from fusion_bulk_export import core
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


if __name__ == '__main__':
    unittest.main()
