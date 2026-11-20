"""--restart-fusion: when the launcher may close and reopen Fusion, with fakes for every process
action (no real process is touched)."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

from tests import fake_adsk as fake

spec = importlib.util.spec_from_file_location('autorun_launcher_restart', Path(__file__).parent.parent / 'scripts' / 'autorun.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)

fake.install()
from fusion_bulk_export.autorun import open_document_facts  # noqa: E402


class FakeFusion:
    """Fusion's process as the launcher sees it. quits: whether a polite quit ends it; the clock
    moves with sleep()."""
    def __init__(self, running=True, quits=True, killable=True, addin=True):
        self.is_running, self.quits, self.killable, self.addin = running, quits, killable, addin
        self.actions = []
        self.now = 0.0
        self.started = False  # the add-in's heartbeat since the restart began

    def running(self):
        return self.is_running

    def quit(self):
        self.actions.append('quit')
        if self.quits:
            self.is_running = False

    def kill(self):
        self.actions.append('kill')
        if self.killable:
            self.is_running = False

    def launch(self):
        self.actions.append('launch')
        self.is_running = True
        self.started = self.addin

    def started_since(self, moment):
        return self.started

    def sleep(self, seconds):
        self.now += seconds

    def clock(self):
        return self.now


def quiet(function, *args, **kwargs):
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        value = function(*args, **kwargs)
    return value, stream.getvalue()


MEMORY = launcher.EXIT_MEMORY
GONE = launcher.EXIT_FUSION_GONE


class DecisionTests(unittest.TestCase):
    def decide(self, status=MEMORY, result=None, restarts=0, limit=20, exported=60):
        return launcher.restart_decision(status, dict(open_documents_with_work=0) if result is None else result,
                                         restarts, limit, exported)

    def test_a_memory_stop_with_nothing_open_restarts(self):
        self.assertEqual(self.decide()[0], True)

    def test_refuses_when_documents_are_open_or_unknown(self):
        allowed, line = self.decide(result=dict(open_documents_with_work=2))
        self.assertFalse(allowed)
        self.assertIn('2 open documents in it hold a saved design or unsaved changes', line)
        allowed, line = self.decide(result=dict(open_documents_with_work=1))
        self.assertIn('1 open document in it holds', line)
        for result in ({}, dict(open_documents_with_work=None)):
            self.assertFalse(self.decide(result=result)[0])

    def test_restart_cap(self):
        self.assertTrue(self.decide(restarts=19)[0])
        allowed, line = self.decide(restarts=20)
        self.assertFalse(allowed)
        self.assertIn('restarted 20 times', line)

    def test_loop_guard_needs_fifty_designs_since_the_last_start(self):
        allowed, line = self.decide(exported=49)
        self.assertFalse(allowed)
        self.assertEqual(line, launcher.EARLY_MEMORY_LINE % (49, 'designs'))
        self.assertEqual(self.decide(exported=1)[1], launcher.EARLY_MEMORY_LINE % (1, 'design'))
        self.assertTrue(self.decide(exported=50)[0])
        self.assertFalse(self.decide(status=GONE, result={}, exported=10)[0])

    def test_a_crash_restarts_without_a_result(self):
        self.assertTrue(self.decide(status=GONE, result={})[0])

    def test_other_endings_never_restart(self):
        for status in (0, 1, launcher.INTERRUPTED, launcher.EXIT_NO_ADDIN):
            self.assertEqual(self.decide(status=status), (False, None))


class RestartTests(unittest.TestCase):
    def test_polite_quit_then_launch(self):
        fusion = FakeFusion()
        self.assertIsNone(quiet(launcher.restart_fusion, fusion, MEMORY, force=False)[0])
        self.assertEqual(fusion.actions, ['quit', 'launch'])

    def test_no_force_without_the_force_option(self):
        fusion = FakeFusion(quits=False)
        status, out = quiet(launcher.restart_fusion, fusion, MEMORY, force=False)
        self.assertEqual(status, MEMORY)
        self.assertEqual(fusion.actions, ['quit'])
        self.assertGreaterEqual(fusion.now, launcher.QUIT_SECONDS)
        self.assertIn('did not quit within 60 s', out)

    def test_force_kills_only_after_the_polite_wait(self):
        fusion = FakeFusion(quits=False)
        self.assertIsNone(quiet(launcher.restart_fusion, fusion, MEMORY, force=True)[0])
        self.assertEqual(fusion.actions, ['quit', 'kill', 'launch'])
        fusion = FakeFusion(quits=False, killable=False)
        self.assertEqual(quiet(launcher.restart_fusion, fusion, MEMORY, force=True)[0], MEMORY)
        self.assertNotIn('launch', fusion.actions)

    def test_after_a_crash_fusion_is_only_opened(self):
        fusion = FakeFusion(running=False)
        self.assertIsNone(quiet(launcher.restart_fusion, fusion, GONE, force=True)[0])
        self.assertEqual(fusion.actions, ['launch'])
        fusion = FakeFusion(running=True)  # the user opened it again meanwhile: never closed
        fusion.started = True  # its add-in keeps writing the heartbeat
        self.assertIsNone(quiet(launcher.restart_fusion, fusion, GONE, force=True)[0])
        self.assertEqual(fusion.actions, [])

    def test_the_add_in_not_starting_stops_with_its_own_status(self):
        fusion = FakeFusion(addin=False)
        status, out = quiet(launcher.restart_fusion, fusion, MEMORY, force=False)
        self.assertEqual(status, launcher.EXIT_NO_ADDIN)
        self.assertIn('Tick its Run on Startup box first', out)
        self.assertIn('Then run the same command again.', out)
        self.assertNotIn('keeps waiting', out)
        self.assertGreaterEqual(fusion.now, launcher.MANUAL_HINT_SECONDS)


class LoopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)

    def runs(self, outcomes, per_run=60, work=0):
        """A run() that exports per_run designs each time and ends with the next outcome."""
        state = dict(done=0)
        remaining = list(outcomes)

        def run():
            state['done'] += per_run
            (self.output / 'manifest.json').write_text(json.dumps(dict(entries=[
                dict(kind='design', status='done', id=str(n)) for n in range(state['done'])])))
            status = remaining.pop(0)
            (self.output / 'autorun-result.json').write_text(json.dumps(dict(
                stopped_code='memory' if status == MEMORY else None, open_documents_with_work=work)))
            return status
        return run, remaining

    def test_memory_stops_are_restarted_until_the_job_finishes(self):
        run, remaining = self.runs([MEMORY, GONE, MEMORY, 0])
        fusion = FakeFusion()
        status, out = quiet(launcher.with_restarts, run, fusion, self.output)
        self.assertEqual(status, 0)
        self.assertEqual(remaining, [])
        self.assertEqual(out.count('Fusion restarted ('), 3)

    def test_the_cap_ends_the_loop(self):
        run, remaining = self.runs([MEMORY] * 5)
        status, out = quiet(launcher.with_restarts, run, FakeFusion(), self.output, limit=2)
        self.assertEqual(status, MEMORY)
        self.assertEqual(len(remaining), 2)  # three runs: the first and two restarts

    def test_few_designs_per_run_ends_the_loop(self):
        run, remaining = self.runs([MEMORY] * 3, per_run=10)
        fusion = FakeFusion()
        status, out = quiet(launcher.with_restarts, run, fusion, self.output)
        self.assertEqual((status, len(remaining)), (MEMORY, 2))
        self.assertEqual(fusion.actions, [])  # Fusion is not touched
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(lines, [launcher.EARLY_MEMORY_LINE % (10, 'designs')])
        self.assertIn("Fusion's memory is high for another reason", lines[0])

    def test_a_memory_stop_soon_after_a_restart_ends_the_loop(self):
        # 60 designs, a restart, then a stop after 20 more: no second restart.
        state = dict(done=0)
        outcomes = [(60, MEMORY), (20, MEMORY), (60, 0)]

        def run():
            more, status = outcomes.pop(0)
            state['done'] += more
            (self.output / 'manifest.json').write_text(json.dumps(dict(entries=[
                dict(kind='design', status='done', id=str(n)) for n in range(state['done'])])))
            (self.output / 'autorun-result.json').write_text(json.dumps(dict(
                stopped_code='memory', open_documents_with_work=0)))
            return status
        fusion = FakeFusion()
        status, out = quiet(launcher.with_restarts, run, fusion, self.output)
        self.assertEqual(status, MEMORY)
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(fusion.actions, ['quit', 'launch'])
        self.assertIn("after only 20 designs since Fusion was started: Fusion's memory is high for another reason",
                      out)
        self.assertIn('75', launcher.__doc__)

    def test_open_documents_stop_the_loop_and_fusion_is_left_alone(self):
        run, _ = self.runs([MEMORY, 0], work=1)
        fusion = FakeFusion()
        status, _ = quiet(launcher.with_restarts, run, fusion, self.output)
        self.assertEqual((status, fusion.actions), (MEMORY, []))


class CrashDetectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out, self.installed = self.root / 'out', self.root / 'inst'
        self.out.mkdir()
        self.installed.mkdir()

    def test_fusion_gone_mid_run_is_detected(self):
        beat = self.installed / 'autorun.alive'
        beat.write_text('{}')
        polls = []

        def sleep(_):
            polls.append(1)
            if len(polls) == 2:  # Fusion dies: its heartbeat goes stale
                old = time.time() - 600
                os.utime(beat, (old, old))
        status, out = quiet(launcher.wait, self.installed, self.out, 86400, sleep=sleep, clock=lambda: 0,
                            running=lambda: False)
        self.assertEqual(status, GONE)
        self.assertIn('Fusion closed before the export finished', out)

    def test_a_busy_fusion_with_a_stale_heartbeat_is_not_a_crash(self):
        beat = self.installed / 'autorun.alive'
        beat.write_text('{}')
        polls = []

        def sleep(_):
            polls.append(1)
            if len(polls) == 2:
                old = time.time() - 600
                os.utime(beat, (old, old))
            if len(polls) == 6:
                (self.out / 'autorun-result.json').write_text(json.dumps(dict(mode='export', counts={}, errors=[])))
        status, _ = quiet(launcher.wait, self.installed, self.out, 86400, sleep=sleep, clock=lambda: 0,
                          running=lambda: True)
        self.assertEqual(status, 0)


class HelpTests(unittest.TestCase):
    def test_the_flag_is_opt_in_and_its_risk_is_in_the_help(self):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), self.assertRaises(SystemExit):
            launcher.main(['export', '--help'])
        text = ' '.join(stream.getvalue().split())
        self.assertIn('--restart-fusion', text)
        # Its help is one plain sentence, and it names the risk.
        flag = text[text.rindex('--restart-fusion [force]'):text.rindex('--max-restarts N')]
        sentence = flag.split(']', 1)[1].strip()
        self.assertEqual(sentence.count('. '), 0, sentence)
        self.assertTrue(sentence.endswith('.'), sentence)
        self.assertIn('closes and reopens Fusion by itself', sentence)
        self.assertIn('losing anything unsaved', sentence)
        self.assertLess(len(sentence.split()), 25, sentence)  # one SHORT sentence
        # Every exit status is listed in "export --help" itself.
        for status in ('0 ', '1 ', '75 ', '76 ', '77 ', '130 '):
            self.assertIn(status, text[text.index('Exit status:'):])
        args = NS()
        parser_args = []
        with patch.object(launcher, 'start', side_effect=lambda a: parser_args.append(a) or 0):
            launcher.main(['export', 'out'])
            launcher.main(['export', 'out', '--restart-fusion'])
            launcher.main(['export', 'out', '--restart-fusion=force', '--max-restarts', '3'])
        self.assertEqual([a.restart_fusion for a in parser_args], [None, 'polite', 'force'])
        self.assertEqual(parser_args[2].max_restarts, 3)
        del args

    def test_without_the_flag_nothing_closes_fusion(self):
        args = launcher.argparse.Namespace(output='/tmp/x', command='export', project=None, limit=None, folder=None,
                                           designs_only=False, timeout=10, restart_fusion=None)
        with patch.object(launcher, 'system', return_value='Darwin'), \
                patch.object(launcher, 'root_problem', return_value=None), \
                patch.object(launcher, 'run_job', return_value=MEMORY), \
                patch.object(launcher, 'with_restarts') as loop:
            self.assertEqual(launcher.start(args), MEMORY)
        loop.assert_not_called()


class OpenDocumentTests(unittest.TestCase):
    def test_only_empty_untitled_documents_count_as_free(self):
        untitled = NS(dataFile=None, isModified=False)
        edited = NS(dataFile=None, isModified=True)
        design = NS(dataFile=fake.File(), isModified=False)

        class Unreadable:
            @property
            def dataFile(self):
                raise RuntimeError('3 : no data file')

            isSaved = False
            isModified = False
        app = NS(documents=fake.Collection(untitled, Unreadable()))
        self.assertEqual(open_document_facts(app), dict(open_documents=2, open_documents_with_work=0))
        app = NS(documents=fake.Collection(untitled, edited, design))
        self.assertEqual(open_document_facts(app), dict(open_documents=3, open_documents_with_work=2))
        self.assertEqual(open_document_facts(NS()), dict(open_documents=None, open_documents_with_work=None))


if __name__ == '__main__':
    unittest.main()
