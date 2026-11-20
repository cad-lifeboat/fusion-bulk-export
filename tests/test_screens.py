"""docs/screens.py draws the README's dialog illustrations: the words in them are the add-in's."""
import importlib.util
from pathlib import Path
import re
import unittest

from tests import fake_adsk as fake

fake.install()

from fusion_bulk_export import command, picker  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('screens', ROOT / 'docs' / 'screens.py')
screens = importlib.util.module_from_spec(spec)
spec.loader.exec_module(screens)


class ScreensTests(unittest.TestCase):
    def test_dialog_words_match_the_add_in(self):
        self.assertEqual(screens.OTHER_FILES, picker.OTHER_FILES)
        self.assertEqual(screens.CHOOSE, picker.CHOOSE)
        self.assertTrue(screens.VERSION.startswith('Fusion Bulk Export '))
        page = (ROOT / 'FusionBulkExport' / 'ui' / 'tree.html').read_text(encoding='utf-8')
        hint = re.search(r'<div class="hint">(.*?)</div>', page, re.S).group(1)
        self.assertEqual(screens.TREE_HINT, ' '.join(hint.split()))
        self.assertIn('placeholder="%s"' % screens.FILTER, page)
        self.assertEqual(screens.OPEN_CHOICES, command.OPEN_CHOICES.strip())

    def test_progress_text_is_what_the_add_in_writes(self):
        self.assertEqual(command.describe(screens.PROGRESS_STATE).replace('%%', '%'), screens.PROGRESS_TEXT)

    def test_drawings_render_and_name_nothing_real(self):
        for draw in screens.SCREENS.values():
            svg = draw()
            self.assertTrue(svg.startswith('<svg'))
            self.assertIn('Fusion Bulk Export', svg)
        self.assertIn('Run again to retry the 1 failed.', screens.summary_lines())


if __name__ == '__main__':
    unittest.main()
