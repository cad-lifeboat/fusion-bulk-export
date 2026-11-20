"""The tree page: its logic (run with node when node is installed) and its layout contract."""
from pathlib import Path
import re
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / 'FusionBulkExport' / 'ui' / 'tree.html'


@unittest.skipUnless(shutil.which('node'), 'node is not installed')
class TreeLogicTests(unittest.TestCase):
    def test_tree_logic(self):
        result = subprocess.run(['node', str(ROOT / 'tests' / 'tree_logic_test.js')],
                                capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        for part in ('tree logic ok', 'errors and retry ok', 'keyboard ok', 'page wiring ok'):
            self.assertIn(part, result.stdout)


def rules(css):
    """{(media or '', selector): {property: value}} for the page's style sheet. Simple, as the
    page's own CSS is: no nested @media, no braces in values."""
    found = {}

    def read(block, media):
        for selectors, body in re.findall(r'([^{}@]+)\{([^{}]*)\}', block):
            properties = {}
            for declaration in body.split(';'):
                if ':' in declaration:
                    name, value = declaration.split(':', 1)
                    properties[name.strip()] = ' '.join(value.split())
            for selector in selectors.split(','):
                found.setdefault((media, ' '.join(selector.split())), {}).update(properties)
    css = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    for query, block in re.findall(r'@media([^{]*)\{((?:[^{}]*\{[^{}]*\})*)\s*\}', css):
        read(block, ' '.join(query.split()))
    read(re.sub(r'@media[^{]*\{(?:[^{}]*\{[^{}]*\})*\s*\}', '', css), '')
    return found


class TreePageLayoutTests(unittest.TestCase):
    """Pixels cannot be measured here, so the CSS rules that keep the page sane from about 220 px
    to 900 px wide are checked instead (real Fusion once showed the page in a 90 px column)."""
    def setUp(self):
        page = PAGE.read_text(encoding='utf-8')
        self.page = page
        self.css = rules(re.search(r'<style>(.*?)</style>', page, re.S).group(1))

    def rule(self, selector, media=''):
        self.assertIn((media, selector), self.css)
        return self.css[(media, selector)]

    def test_scrollbars_follow_the_theme(self):
        # Fusion's dark theme showed a bright white scrollbar track: the page names its colour
        # scheme for both themes and styles its scrollbars with its own colours.
        self.assertEqual(self.rule(':root')['color-scheme'], 'dark')
        self.assertEqual(self.rule(':root', '(prefers-color-scheme: light)')['color-scheme'], 'light')
        self.assertEqual(self.rule('html')['scrollbar-color'], 'var(--line) transparent')
        self.assertEqual(self.rule('::-webkit-scrollbar-track')['background'], 'transparent')
        self.assertEqual(self.rule('::-webkit-scrollbar')['background'], 'transparent')
        self.assertIn('var(--line)', self.rule('::-webkit-scrollbar-thumb')['background'])
        self.assertNotIn('#fff', self.rule('::-webkit-scrollbar-thumb')['background'])

    def test_height_does_not_depend_on_the_content(self):
        # The same minimum as the dialog sets on the input (picker.TREE_MIN_HEIGHT), as an absolute
        # length on both html and body: an empty or loading page is as tall as a full one.
        source = (ROOT / 'FusionBulkExport' / 'fusion_bulk_export' / 'picker.py').read_text(encoding='utf-8')
        minimum = int(re.search(r'^TREE_MIN_HEIGHT = (\d+)', source, re.M).group(1))
        for selector in ('html', 'body'):
            self.assertEqual(self.rule(selector)['min-height'], '%dpx' % minimum)
            self.assertNotIn('height', self.rule(selector))
            self.assertNotIn('max-height', self.rule(selector))

    def test_never_a_horizontal_scrollbar(self):
        self.assertEqual(self.rule('body')['overflow-x'], 'hidden')
        self.assertEqual(self.rule('html')['overflow-x'], 'hidden')
        self.assertEqual(self.rule('.row')['min-width'], '0')

    def test_long_names_end_in_an_ellipsis(self):
        label = self.rule('.label')
        self.assertEqual(label['min-width'], '0')
        self.assertEqual(label['white-space'], 'nowrap')
        self.assertEqual(label['overflow'], 'hidden')
        self.assertEqual(label['text-overflow'], 'ellipsis')
        tree_js = (PAGE.parent / 'tree.js').read_text(encoding='utf-8')
        self.assertIn('line.title = n.name', tree_js)  # the whole name as the row's tooltip

    def test_counts_and_badges_stay_whole(self):
        for selector in ('.count', '.badge'):
            self.assertEqual(self.rule(selector)['white-space'], 'nowrap', selector)
            self.assertEqual(self.rule(selector)['flex'], 'none', selector)

    def test_readable_font_and_full_width_filter(self):
        size = int(re.search(r'(\d+)px', self.rule('body')['font']).group(1))
        self.assertTrue(12 <= size <= 13, size)
        self.assertEqual(self.rule('#filter')['width'], 'calc(100% - 8px)')
        self.assertEqual(self.rule('#filter')['box-sizing'] if 'box-sizing' in self.rule('#filter')
                         else self.rule('*')['box-sizing'], 'border-box')

    def test_narrow_page_drops_icons_not_names(self):
        narrow = [media for media, _ in self.css if 'max-width' in media]
        self.assertEqual(len(set(narrow)), 1, narrow)
        width = int(re.search(r'max-width:\s*(\d+)px', narrow[0]).group(1))
        self.assertTrue(220 <= width < 260, width)
        self.assertEqual(self.rule('.icon', narrow[0])['display'], 'none')
        self.assertNotIn((narrow[0], '.label'), self.css)

    def test_hint_is_short(self):
        hint = re.search(r'<div class="hint">(.*?)</div>', self.page, re.S).group(1)
        words = ' '.join(re.sub(r'<[^>]+>', '', hint).split())
        self.assertLessEqual(len(words), 130)  # two lines at the dialog's usual width


SUMMARY = ROOT / 'FusionBulkExport' / 'ui' / 'summary.html'


@unittest.skipUnless(shutil.which('node'), 'node is not installed')
class SummaryLogicTests(unittest.TestCase):
    def test_summary_logic_and_page(self):
        result = subprocess.run(['node', str(ROOT / 'tests' / 'summary_logic_test.js')],
                                capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        for part in ('summary logic ok', 'summary keys ok', 'summary page ok'):
            self.assertIn(part, result.stdout)


class SummaryPageLayoutTests(unittest.TestCase):
    """The results page: the tree page's colours, light and dark, no sideways scrolling, names
    that end in an ellipsis, a folder path that can be selected, nothing loaded from outside."""
    def setUp(self):
        self.page = SUMMARY.read_text(encoding='utf-8')
        self.css = rules(re.search(r'<style>(.*?)</style>', self.page, re.S).group(1))

    def test_light_and_dark_with_the_tree_pages_colours(self):
        tree = rules(re.search(r'<style>(.*?)</style>', PAGE.read_text(encoding='utf-8'), re.S).group(1))
        light = '(prefers-color-scheme: light)'
        for media in ('', light):
            for name in ('--text', '--muted', '--line', '--tick', '--bad', '--good'):
                self.assertEqual(self.css[(media, ':root')][name], tree[(media, ':root')][name], (media, name))

    def test_never_a_horizontal_scrollbar(self):
        self.assertEqual(self.css[('', 'html')]['overflow-x'], 'hidden')
        self.assertIn('ellipsis', self.css[('', '.failure .name')]['text-overflow'])
        self.assertEqual(self.css[('', '.folder')]['overflow-wrap'], 'anywhere')
        self.assertIn('minmax(170px', self.css[('', '.counts')]['grid-template-columns'])

    def test_folder_can_be_selected_and_nothing_external(self):
        self.assertEqual(self.css[('', '.folder')]['user-select'], 'text')
        self.assertNotIn('http', self.page)
        self.assertIn('<script src="summary.js"></script>', self.page)
        script = (ROOT / 'FusionBulkExport' / 'ui' / 'summary.js').read_text(encoding='utf-8')
        self.assertNotIn('innerHTML', script)
        self.assertNotIn('insertAdjacentHTML', script)
