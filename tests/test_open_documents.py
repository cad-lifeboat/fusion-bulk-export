"""Documents left open in Fusion: after each item and at the end of a run, the add-in closes the
documents it opened or caused to be opened (linked designs, late ones, other versions), never
one that was open when the run began, and never leaves an extra empty "Untitled" it could close."""
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

from tests import fake_adsk as fake
from fusion_bulk_export.core import Exporter

fake.install()
from fusion_bulk_export import fusion  # noqa: E402
from fusion_bulk_export.fusion import FusionAdapter  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fusion-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.documents = fake.Documents()
        self.documents.cascade = False  # Fusion keeps linked designs open after their assembly closes
        self.app = NS(documents=self.documents)
        self.adapter = FusionAdapter(self.app)
        for kept in (fusion.LEFT_OPEN, fusion.BROKEN, fake.PENDING):
            kept.clear()
            self.addCleanup(kept.clear)
        # adsk.doEvents does Fusion's late work (fake.do_events: one pending step per call).
        events = patch.object(fusion.adsk, 'doEvents', fake.do_events)
        events.start()
        self.addCleanup(events.stop)

    def run_export(self, files, **kwargs):
        exporter = Exporter(self.root, self.adapter)
        counts = exporter.run(fake.data(files), **kwargs)
        self.manifest = json.loads((self.root / 'manifest.json').read_text(encoding='utf-8'))
        return exporter, counts

    def user_has_open(self, file, modified=False):
        document = fake.Document(file, self.documents)
        document.isModified = modified
        self.documents.items.append(document)
        return document

    def statuses(self):
        return [entry['status'] for entry in self.manifest['entries']]


class ItemCleanupTests(Base):
    def test_a_link_to_another_version_of_an_open_design_is_closed_and_the_users_is_not(self):
        part = fake.File('Part', 'part', version=3)
        mine = self.user_has_open(part)
        assembly = fake.File('Assembly', 'assembly')
        assembly.childReferences = fake.Collection(fake.File('Part', 'part', version=2))  # pinned to v2
        exporter, counts = self.run_export([assembly])
        self.assertEqual(counts['exported'], 1)
        self.assertEqual(self.documents.items, [mine])
        self.assertEqual((mine.closed, mine.closes), (False, []))
        self.assertEqual((exporter.run_info['documents_at_start'], exporter.run_info['documents_at_end']), (1, 1))

    def test_a_link_inside_a_linked_design_is_closed(self):
        child = fake.File('Sub-assembly', 'sub', references=1)
        assembly = fake.File('Assembly', 'assembly')
        assembly.childReferences = fake.Collection(child)
        _, counts = self.run_export([assembly])
        self.assertEqual(counts['exported'], 1)
        nested = child.childReferences.item(0).documents
        self.assertEqual(len(nested), 1)  # Fusion opened it, with the linked design
        self.assertTrue(nested[0].closed)
        self.assertEqual(self.documents.items, [])

    def test_a_linked_design_fusion_opens_a_moment_later_is_closed_with_its_item(self):
        assembly = fake.File('Assembly', 'assembly', references=2)
        assembly.lazy_references = 0  # at the first doEvents
        exporter, _ = self.run_export([assembly])
        self.assertEqual(len(assembly.childReferences.item(0).documents), 1)
        self.assertEqual(self.documents.items, [])
        self.assertEqual(exporter.run_info['closed_leftovers'], 0)

    def test_a_linked_design_opened_after_its_item_is_closed_later_in_the_run(self):
        assembly, other = fake.File('Assembly', 'assembly', references=1), fake.File('Other', 'other')
        assembly.lazy_references = fusion.SETTLE_DOCUMENTS + 1  # after the item's own cleanup
        _, counts = self.run_export([assembly, other])
        self.assertEqual(counts['exported'], 2)
        self.assertEqual(len(assembly.childReferences.item(0).documents), 1)
        self.assertEqual(self.documents.items, [])

    def test_a_linked_design_opened_after_the_last_item_is_closed_as_the_run_ends(self):
        assembly = fake.File('Assembly', 'assembly', references=1)
        assembly.lazy_references = fusion.SETTLE_DOCUMENTS + 1
        exporter, _ = self.run_export([assembly])
        self.assertEqual(len(assembly.childReferences.item(0).documents), 1)
        self.assertEqual(self.documents.items, [])
        self.assertEqual((exporter.run_info['documents_at_end'], exporter.run_info['closed_leftovers']), (0, 1))
        self.assertIn('left open by the run and closed: 1', (self.root / 'export.log').read_text(encoding='utf-8'))

    def test_a_linked_design_still_open_from_an_earlier_item_is_exported_from_a_fresh_copy(self):
        # Fusion marks the linked design modified as it opens it: that is no reason to fail it
        # as "open in Fusion with unsaved changes" when it comes up as an item of its own.
        assembly = fake.File('Assembly', 'assembly', references=1)
        child = assembly.childReferences.item(0)
        child.modified_on_open = True
        assembly.lazy_references = fusion.SETTLE_DOCUMENTS + 1
        _, counts = self.run_export([assembly, child])
        self.assertEqual(self.statuses(), ['done', 'done'], self.manifest['entries'])
        self.assertEqual(self.documents.items, [])

    def test_an_error_during_the_export_leaves_nothing_open(self):
        assembly = fake.File('Assembly', 'assembly', references=2)
        assembly.childReferences.item(0).childReferences = fake.Collection(fake.File('Deep', 'deep'))
        assembly.failures = 10 ** 6
        _, counts = self.run_export([assembly])
        self.assertEqual(counts['failed'], 1)
        self.assertEqual(self.documents.items, [])

    def test_a_shown_linked_design_nobody_changed_is_closed_a_changed_one_is_not(self):
        self.documents.shown_references = True
        assembly = fake.File('Assembly', 'assembly', references=2)
        changed = assembly.childReferences.item(1)
        changed.modified_on_open = True
        self.run_export([assembly])
        self.assertEqual([document.dataFile.id for document in self.documents.items], [changed.id])

    def test_documents_open_when_the_run_began_are_never_closed(self):
        part, other = fake.File('Part', 'part'), fake.File('Other', 'other', references=1)
        mine = self.user_has_open(part)
        edited = fake.LocalDocument('Untitled', self.documents, parts=2)
        edited.isModified = True
        empty = fake.LocalDocument('Untitled(1)', self.documents)  # empty, but the user's
        hidden = fake.Document(fake.File('Script work', 'elsewhere'), self.documents, visible=False)
        self.documents.items += [edited, empty, hidden]
        before = list(self.documents.items)
        exporter, counts = self.run_export([part, other])
        self.assertEqual(counts['exported'], 2)
        self.assertEqual(self.documents.items, before)
        self.assertTrue(all(not document.closes for document in before))
        self.assertEqual((exporter.run_info['documents_at_start'], exporter.run_info['documents_at_end'],
                          exporter.run_info['closed_leftovers']), (4, 4, 0))

    def test_the_users_document_saved_as_a_new_version_during_the_run_is_never_closed(self):
        part = fake.File('Part', 'part')
        mine = self.user_has_open(part)
        other = fake.File('Other', 'other')
        original = other.on_close

        def saved(document):  # the user saves their design while the export runs
            mine.dataFile = fake.File('Part', 'part', version=4)
        other.on_close = saved
        self.run_export([other])
        other.on_close = original
        self.assertIn(mine, self.documents.items)
        self.assertEqual(mine.closes, [])


class UntitledTests(Base):
    def test_fusion_replacing_its_empty_start_tab_leaves_one_untitled_that_is_kept(self):
        self.documents.untitled = 'replace'
        start = fake.LocalDocument('Untitled', self.documents)
        self.documents.items.append(start)
        exporter, counts = self.run_export([fake.File('Part', 'part'), fake.File('Other', 'other')])
        self.assertEqual(counts['exported'], 2)
        self.assertEqual(start.closes, [])  # Fusion closed it, not the add-in
        names = [document.name for document in self.documents.items]
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].startswith('Untitled('))
        self.assertEqual(self.documents.items[0].closes, [])  # the only shown document: Fusion would make another
        self.assertEqual((exporter.run_info['documents_at_start'], exporter.run_info['documents_at_end'],
                          exporter.run_info['closed_leftovers']), (1, 1, 0))

    def test_an_untitled_fusion_makes_when_a_hidden_active_document_closes_is_closed(self):
        self.documents.untitled = 'extra'
        start = fake.LocalDocument('Untitled', self.documents)
        mine = self.user_has_open(fake.File('Mine', 'mine'))
        self.documents.items.insert(0, start)
        exporter, counts = self.run_export([fake.File('Part', 'part'), fake.File('Other', 'other')])
        self.assertEqual(counts['exported'], 2)
        self.assertEqual(self.documents.made, 2)  # one per closed hidden design
        self.assertEqual(self.documents.items, [start, mine])
        self.assertEqual((start.closes, mine.closes), ([], []))
        self.assertEqual((exporter.run_info['documents_at_start'], exporter.run_info['documents_at_end'],
                          exporter.run_info['closed_leftovers']), (2, 2, 2))

    def test_an_untitled_with_something_in_it_is_never_closed(self):
        self.documents.untitled = 'extra'
        mine = self.user_has_open(fake.File('Mine', 'mine'))
        original = self.documents.add_untitled

        def drawn_in():  # the user sketched in the new Untitled before the run ended
            document = original()
            document.products = fake.LocalDocument(parts=1).products
            return document
        self.documents.add_untitled = drawn_in
        self.run_export([fake.File('Part', 'part')])
        self.assertEqual(len(self.documents.items), 2)
        self.assertIn(mine, self.documents.items)

    def test_outside_a_run_nothing_is_swept(self):
        self.assertEqual(self.adapter.finish_run(), (0, 0))


if __name__ == '__main__':
    unittest.main()
