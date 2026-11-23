"""docs/screens.py draws the README's dialog illustrations: the words in them are the add-in's."""
import importlib.util
from pathlib import Path
import re
import unittest

from tests import fake_adsk as fake

fake.install()

from fusion_bulk_export import command, picker, results  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('screens', ROOT / 'docs' / 'screens.py')
screens = importlib.util.module_from_spec(spec)
spec.loader.exec_module(screens)


class ScreensTests(unittest.TestCase):
    def test_dialog_words_match_the_add_in(self):
        self.assertEqual(screens.OTHER_FILES, picker.OTHER_FILES)
        self.assertEqual(screens.STEP, picker.STEP)
        self.assertEqual(screens.TOOL_LIBRARIES, picker.TOOL_LIBRARIES)
        self.assertEqual(screens.TOOL_LIBRARIES_NOTE, picker.TOOL_LIBRARIES_NOTE)
        self.assertEqual(screens.F3Z, picker.F3Z)
        self.assertEqual(screens.F3Z_NOTE, picker.F3Z_NOTE)
        self.assertEqual(screens.OLDER, picker.OLDER)
        self.assertEqual(('move', screens.OLDER_CHOICE), picker.OLDER_CHOICES[0])  # the default, first
        self.assertEqual(screens.OLDER_NOTE, picker.OLDER_NOTES['move'])
        self.assertFalse(hasattr(screens, 'DRAWINGS'))  # drawing export was removed
        self.assertEqual(screens.CHOOSE, picker.CHOOSE)
        self.assertTrue(screens.VERSION.startswith('Fusion Bulk Export '))
        page = (ROOT / 'FusionBulkExport' / 'ui' / 'tree.html').read_text(encoding='utf-8')
        hint = re.search(r'<div class="hint">(.*?)</div>', page, re.S).group(1)
        self.assertEqual(screens.TREE_HINT, " ".join(re.sub(r"<[^>]+>", "", hint).split()))
        self.assertIn('placeholder="%s"' % screens.FILTER, page)
        self.assertEqual(screens.RESULTS_TITLE, results.TITLE)
        self.assertEqual(screens.CLOSE, results.CLOSE)
        self.assertEqual(screens.PAGE_BUTTONS, tuple(label for _, label in results.BUTTONS))
        self.assertEqual(screens.SAVED_IN, results.SAVED_IN)

    def test_progress_text_is_what_the_add_in_writes(self):
        self.assertEqual(command.describe(screens.PROGRESS_STATE).replace('%%', '%'), screens.PROGRESS_TEXT)

    def test_drawings_render_and_name_nothing_real(self):
        for draw in screens.SCREENS.values():
            svg = draw()
            self.assertTrue(svg.startswith('<svg'))
            self.assertIn('Fusion Bulk Export', svg)
            self.assertNotIn('drawings as PDF', svg)
        data = screens.summary_data()
        self.assertEqual(data['retry'], 'Run again to retry the 1 failed.')
        self.assertIn(data['headline'], screens.progress_svg())
        for label in screens.PAGE_BUTTONS + (screens.CLOSE,):
            self.assertIn('>%s</text>' % label, screens.progress_svg())
        self.assertNotIn('>Yes</text>', screens.progress_svg())


if __name__ == '__main__':
    unittest.main()
