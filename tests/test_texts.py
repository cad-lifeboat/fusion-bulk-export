"""Guard over every user-facing string of the add-in: no sentence explains which button does
what (buttons are named for what they do), and each dialog message stays short."""
import ast
import io
from pathlib import Path
import re
import tokenize
import unittest

ROOT = Path(__file__).resolve().parents[1] / 'FusionBulkExport'
BUTTON_TALK = re.compile(r'\b(Yes|No|Cancel|OK)\b.*(open|opens|close|closes|stop|stops)', re.I)

# American spelling and usage in everything a user or contributor reads. A checkbox is "checked",
# not "ticked". The job protocol keeps its own spelling: a word glued to a quote, backtick or code
# punctuation (`cancelled`, "cancelled":, run_info['cancelled'], .cancelled(), --tick) is a name
# other programs rely on, not prose.
BRITISH = re.compile(
    r'\b(cancell(ed|ing)|(un)?tick(s|ed|ing)?|grey(s|ed|ing)?|colours?|colour(ed|ing)'
    r'|behaviours?|queueing|(recogn|organ|real|summar|initial|normal|custom|optim|prioriti?|author|util)'
    r'is(e|es|ed|ing|ation|ations)|(model|label|travel|cancel|signal|level|total)l(ed|ing)'
    r'|judgement|honour(s|ed|ing)?|favour(s|ed|ite)?|centre(s|d)?|centred|towards|afterwards|whilst'
    r'|amongst|licence|catalogue|analys(e|es|ed|ing)|programmes?|learnt|spelt)\b', re.I)
GLUED_BEFORE, GLUED_AFTER = set('\'"`._-[/'), set('\'"`_-[(=')
# Fixed fragments where the protocol value stands unquoted: the state column of the reason codes.
SPELLING_ALLOWED = ('| `cancelled` | cancelled |',)


def british(text):
    """The British spellings in a piece of prose, names glued to code punctuation left out."""
    for fragment in SPELLING_ALLOWED:
        text = text.replace(fragment, '')
    found = []
    for match in BRITISH.finditer(text):
        word, start, end = match.group(0), match.start(), match.end()
        before = text[start - 1] if start else ''
        after = text[end] if end < len(text) else ''
        if (before and before in GLUED_BEFORE) or (after and after in GLUED_AFTER) or word.isupper():
            continue
        found.append(word)
    return found


def prose(path):
    """(line, text) of what a reader sees in a file: for Python, its string literals (docstrings
    included) and comments; for any other text file, every line."""
    source = path.read_text(encoding='utf-8')
    if path.suffix != '.py':
        yield from enumerate(source.splitlines(), 1)
        return
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value != 'cancelled':
            yield node.lineno, node.value
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            yield token.start[0], token.string


def user_facing():
    top = ROOT.parent
    yield from sorted(top.glob('*.md'))
    yield from sorted(path for path in top.glob('docs/*') if path.suffix in ('.md', '.json', '.svg', '.py'))
    yield from sorted(path for path in top.glob('packaging/*') if path.is_file())
    yield from sorted(top.glob('.github/**/*.yml'))
    yield from sorted(top.glob('scripts/*.py'))
    yield from sources()
    yield from sorted((ROOT / 'ui').glob('*.*'))


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

    def test_no_count_says_s_in_brackets(self):
        # "1 design(s)" reads badly: every count is written with words.plural().
        pattern = re.compile(r'\((e?s)\)')
        found = []
        scripts = sorted((ROOT.parent / 'scripts').glob('*.py'))
        for path in list(sources()) + scripts + [ROOT.parent / 'docs' / 'screens.py']:
            for line, text in literals(path):
                if pattern.search(text):
                    found.append('%s:%d %s' % (path.name, line, text))
        for path in sorted((ROOT / 'ui').glob('*.*')):
            for number, text in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
                for quoted in re.findall(r"'([^']*)'|\"([^\"]*)\"|>([^<]+)<", text):
                    if pattern.search(''.join(quoted)):
                        found.append('%s:%d %s' % (path.name, number, ''.join(quoted)))
        self.assertEqual(found, [])

    def test_american_spelling(self):
        found = []
        for path in user_facing():
            for line, text in prose(path):
                for word in british(text):
                    found.append('%s:%d %s' % (path.relative_to(ROOT.parent), line, word))
        self.assertEqual(found, [])

    def test_spelling_guard_catches_british_words_and_spares_protocol_names(self):
        self.assertEqual(british('Cancelled after 3 items.'), ['Cancelled'])
        self.assertEqual(british('Tick its Run on Startup box; the colour stays grey.'), ['Tick', 'colour', 'grey'])
        self.assertEqual(british('Untick it, then recognise the behaviour.'), ['Untick', 'recognise', 'behaviour'])
        self.assertEqual(british('state `cancelled`, "cancelled": true, run_info[\'cancelled\'], .cancelled()'), [])
        self.assertEqual(british('var(--tick); EXIT_CANCELLED; progress.wasCancelled; TICK'), [])
        self.assertEqual(british('Canceled. Check its box; the color stays gray; checked toward the end.'), [])

    def test_plural(self):
        from fusion_bulk_export.words import form, plural
        self.assertEqual(plural(1, 'design'), '1 design')
        self.assertEqual(plural(8, 'design'), '8 designs')
        self.assertEqual(plural(0, 'design'), '0 designs')
        self.assertEqual(plural(2, 'entry'), '2 entries')
        self.assertEqual(plural(3, 'whole project'), '3 whole projects')
        self.assertEqual(plural(2, 'STEP file'), '2 STEP files')
        self.assertEqual(form(2, 'uses'), 'use')
        self.assertEqual(form(1, 'uses'), 'uses')
        self.assertEqual(form(2, 'it', 'them'), 'them')

    def test_dialog_messages_are_at_most_two_sentences_each(self):
        from tests import fake_adsk as fake
        fake.install()
        from fusion_bulk_export import command, errors, picker
        texts = [errors.no_hubs().message, errors.no_hubs().next_step, picker.OLD_FUSION,
                 picker.OTHER_FILES_WHY, command.trouble_text('The disk is full.', 'x')]
        texts += list(errors.NEXT_STEP.values())
        for text in texts:
            sentences = [s for s in re.split(r'(?<=[.?!])\s+', text.replace('\n', ' ')) if s.strip()]
            self.assertLessEqual(len(sentences), 3 if text.startswith('The export has paused') else 2, text)


if __name__ == '__main__':
    unittest.main()
