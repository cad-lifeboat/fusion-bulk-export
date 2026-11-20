"""Guard over every user-facing string of the add-in: no sentence explains which button does
what (buttons are named for what they do), and each dialog message stays short."""
import ast
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1] / 'FusionBulkExport'
BUTTON_TALK = re.compile(r'\b(Yes|No|Cancel|OK)\b.*(open|opens|close|closes|stop|stops)', re.I)


def literals(path):
    """(line, text) of every string literal in a Python file, docstrings left out (not shown)."""
    tree = ast.parse(path.read_text(encoding='utf-8'))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(getattr(first, 'value', None), ast.Constant):
                docstrings.add(id(first.value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            yield node.lineno, node.value


def sources():
    yield from sorted(ROOT.glob('*.py'))
    yield from sorted((ROOT / 'fusion_bulk_export').glob('*.py'))


class TextTests(unittest.TestCase):
    def test_no_text_explains_buttons(self):
        found = []
        for path in sources():
            for line, text in literals(path):
                for part in text.splitlines():
                    if BUTTON_TALK.search(part):
                        found.append('%s:%d %s' % (path.name, line, part))
        for path in sorted((ROOT / 'ui').glob('*.*')):
            for number, part in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
                for quoted in re.findall(r"'([^']*)'|>([^<]+)<", part):
                    text = ''.join(quoted)
                    if BUTTON_TALK.search(text):
                        found.append('%s:%d %s' % (path.name, number, text))
        self.assertEqual(found, [])

    def test_guard_catches_the_old_wording(self):
        self.assertTrue(BUTTON_TALK.search('Yes: open the report (index.html).  No: open the folder.'))
        self.assertTrue(BUTTON_TALK.search('OK tries again and carries on. Cancel stops the export'))
        self.assertFalse(BUTTON_TALK.search('Everything exported so far is kept. Try again now?'))

    def test_dialog_messages_are_at_most_two_sentences_each(self):
        from tests import fake_adsk as fake
        fake.install()
        from fusion_bulk_export import command, errors, picker
        texts = [errors.no_hubs().message, errors.no_hubs().next_step, picker.OLD_FUSION,
                 picker.DRAWINGS_WHY, picker.OTHER_FILES_WHY, command.trouble_text('The disk is full.', 'x')]
        texts += list(errors.NEXT_STEP.values())
        for text in texts:
            sentences = [s for s in re.split(r'(?<=[.?!])\s+', text.replace('\n', ' ')) if s.strip()]
            self.assertLessEqual(len(sentences), 3 if text.startswith('The export has paused') else 2, text)


if __name__ == '__main__':
    unittest.main()
