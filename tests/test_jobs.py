"""Job protocol 1 (jobs.py, docs/job-protocol.md): the agent file and its heartbeat, claiming,
validating, queuing and running jobs from ~/.cad-export/jobs, their status files, canceling,
folder_busy, interrupted jobs, each scope (everything, hubs, projects, folders, a design list with
its linked designs), STEP only and inventory. Against the synthetic API, in a temporary home."""
import errno
import importlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from tests import fake_adsk as fake

fake.install()
from fusion_bulk_export import jobs  # noqa: E402

REQUESTED = '2026-11-23T09:31:00+00:00'


def dead_pid():
    """The id of a process that has just ended."""
    process = subprocess.Popen([sys.executable, '-c', 'pass'])
    process.wait()
    return process.pid


class JobTestCase(unittest.TestCase):
    def setUp(self):
        import adsk
        adsk.doEvents = lambda: None
        self.temp = tempfile.TemporaryDirectory(prefix='fusion-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve() / 'home'
        self.home.mkdir()
        self.output = self.home / 'FusionExport'
        self.files = self.account()
        self.app = NS(version='test-version', documents=fake.Documents(), data=self.data, userInterface=NS())
        adsk.core.Application = NS(get=lambda: self.app)
        sleep = patch('fusion_bulk_export.autorun.time.sleep')
        sleep.start()
        self.addCleanup(sleep.stop)
        self.service = jobs.Service('9.9.9', 'test-version', home=self.home)
        self.service.setup()
        self.busy = {'by': None, 'job': None}

    def account(self):
        """hub-1: project-1 (A, B at the root; Sub with a PDF; Parts with an assembly of P1 and P2,
        P2 in Parts/Deep linking P3) and project-2 (P3, C); hub-2: project-3 (E)."""
        f = {name: fake.File(name.upper() if len(name) == 1 else name.capitalize(), name)
             for name in ('a', 'b', 'c', 'e', 'p1', 'p2', 'p3', 'asm')}
        f['pdf'] = fake.File('Sheet.pdf', 'pdf', extension='pdf')
        for parent, children in (('asm', ('p1', 'p2')), ('p2', ('p3',))):
            f[parent].childReferences = fake.Collection(*[f[child] for child in children])
            f[parent].hasChildReferences = True
        deep = fake.folder('Deep', files=[f['p2']], identity='folder-deep')
        parts = fake.folder('Parts', files=[f['asm'], f['p1']], folders=[deep], identity='folder-parts')
        sub = fake.folder('Sub', files=[f['pdf']], identity='folder-sub')
        self.data = fake.data([f['a'], f['b']], [sub, parts])
        hub = self.data.dataHubs.item(0)
        hub.dataProjects.items.append(NS(id='project-2', name='Two', rootFolder=fake.folder(files=[f['p3'], f['c']])))
        other = NS(id='project-3', name='Three', rootFolder=fake.folder(files=[f['e']]))
        self.data.dataHubs.items.append(NS(id='hub-2', name='Second', dataProjects=fake.Collection(other)))
        fake.with_parents(self.data)
        return f

    def submit(self, stem='job-1', **fields):
        job = dict(protocol=1, id=stem, requested=REQUESTED, output_dir=str(self.output))
        job.update(fields)
        path = self.service.jobs / (stem + '.json')
        path.write_bytes(json.dumps(job).encode('utf-8'))
        return path

    def status(self, stem='job-1'):
        return json.loads((self.service.jobs / (stem + '.status.json')).read_bytes().decode('utf-8'))

    def handle(self):
        self.service.handle(self.app, self.busy)

    def manifest(self, folder=None):
        return json.loads(((folder or self.output) / 'manifest.json').read_bytes().decode('utf-8'))

    def exported(self, folder=None):
        return sorted(e['id'] for e in self.manifest(folder)['entries'] if e['status'] == 'done')


class AgentFileTests(JobTestCase):
    def test_folders_are_owner_only_and_inside_the_home(self):
        for folder in (self.service.base, self.service.agents, self.service.jobs):
            self.assertTrue(folder.is_dir())
            if os.name == 'posix':
                self.assertEqual(stat.S_IMODE(folder.stat().st_mode) & 0o077, 0)
        outside = Path(self.temp.name).resolve() / 'elsewhere'
        outside.mkdir()
        other = self.home.parent / 'home2'
        other.mkdir()
        try:
            (other / '.cad-export').symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('no symbolic links here')
        with self.assertRaises(ValueError):
            jobs.Service('9.9.9', 'x', home=other).setup()

    def test_agent_file_fields_heartbeat_and_removal(self):
        clock = [100.0]
        self.service.clock = lambda: clock[0]
        self.service.write_agent(self.busy)
        path = self.service.agents / 'fusion-bulk-export.json'
        agent = json.loads(path.read_bytes().decode('utf-8'))
        self.assertEqual((agent['protocol'], agent['protocols'], agent['agent'], agent['exporter'], agent['version']),
                         (1, [1], 'fusion-bulk-export', 'fusion-bulk-export', '9.9.9'))
        self.assertEqual((agent['fusion_version'], agent['fusion_running'], agent['pid'], agent['busy'], agent['job']),
                         ('test-version', True, os.getpid(), None, None))
        self.assertEqual(agent['jobs_dir'], str(self.service.jobs))
        self.assertIn('step_only', agent['features'])
        self.assertIsNotNone(jobs.parse_time(agent['updated']))
        first = agent['updated']
        clock[0] += 5
        self.service.beat(self.busy)  # not yet: about every 10 s
        self.assertEqual(json.loads(path.read_bytes())['updated'], first)
        clock[0] += 6
        self.busy.update(by='button')
        self.service.beat(self.busy)
        agent = json.loads(path.read_bytes())
        self.assertEqual(agent['busy'], 'button')
        self.assertEqual([name for name in os.listdir(str(self.service.agents))], ['fusion-bulk-export.json'])
        self.service.remove_agent()
        self.assertFalse(path.exists())
        self.service.beat(self.busy)  # gone: written again at once
        self.assertTrue(path.exists())


class ClaimTests(JobTestCase):
    def test_claim_is_exclusive_with_and_without_hard_links(self):
        target = self.service.jobs / 'x.status.json'
        self.assertTrue(jobs.claim_json(target, dict(state='queued')))
        self.assertFalse(jobs.claim_json(target, dict(state='queued', other=True)))
        self.assertEqual(json.loads(target.read_bytes()), dict(state='queued'))
        other = self.service.jobs / 'y.status.json'
        with patch('fusion_bulk_export.jobs.os.link', side_effect=OSError(errno.EPERM, 'no links here')):
            self.assertTrue(jobs.claim_json(other, dict(state='queued')))
            self.assertFalse(jobs.claim_json(other, dict(state='failed')))
        self.assertEqual(json.loads(other.read_bytes()), dict(state='queued'))
        self.assertEqual([n for n in os.listdir(str(self.service.jobs)) if n.startswith('.')], [])

    def test_two_add_ins_claim_one_job_once(self):
        self.submit()
        second = jobs.Service('9.9.9', 'x', home=self.home, pid=self.service.pid + 1)
        second.claim_new(self.busy)
        self.service.claim_new(self.busy)
        self.assertEqual(self.status()['agent_pid'], self.service.pid + 1)
        self.assertEqual((second.queued, self.service.queued), ({'job-1'}, set()))
        self.handle()  # not this add-in's job: never run here
        self.assertEqual(self.status()['state'], 'queued')
        self.assertFalse(self.output.exists())

    def test_files_never_claimed(self):
        jobs_dir = self.service.jobs
        (jobs_dir / '.hidden.json').write_bytes(b'{}')
        (jobs_dir / 'x.json.tmp').write_bytes(b'{}')
        (jobs_dir / 'bad name.json').write_bytes(b'{}')
        (jobs_dir / 'big.json').write_bytes(b' ' * (jobs.MAX_JOB_BYTES + 1))
        try:
            (jobs_dir / 'linked.json').symlink_to(self.submit('real'))
            linked = True
        except (OSError, NotImplementedError):
            linked = False
        self.service.claim_new(self.busy)
        statuses = sorted(n for n in os.listdir(str(jobs_dir)) if n.endswith('.status.json'))
        self.assertEqual(statuses, ['real.status.json'])
        if linked:
            self.assertFalse((jobs_dir / 'linked.status.json').exists())


class ValidationTests(JobTestCase):
    def check_invalid(self, words, stem='job-1', reason='invalid_job', **fields):
        self.submit(stem, **fields)
        self.handle()
        status = self.status(stem)
        self.assertEqual((status['state'], status['reason'], status['exit_code']), ('failed', reason, 1), fields)
        self.assertIn(words, status['message'])
        self.assertIsNotNone(status['finished'])
        self.assertFalse(self.output.exists())
        return status

    def test_unknown_fields_wrong_types_and_bad_combinations_are_refused(self):
        cases = [
            ('Unknown field: color', dict(color='red')),
            ('id must equal', dict(id='other')),
            ('requested', dict(requested='yesterday')),
            ('absolute', dict(output_dir='relative/folder')),
            ('action', dict(action='diagnostics')),
            ('formats', dict(formats=['f3d', 'obj'])),
            ('formats', dict(formats=['step', 'step'])),
            ('formats', dict(formats=[])),
            ('rebuilt_f3z needs', dict(formats=['step'], rebuilt_f3z=True)),
            ('hubs can only', dict(designs=['a'], hubs=['hub-1'])),
            ('designs must be "all"', dict(action='inventory', designs=['a'])),
            ('tool_libraries "only"', dict(designs=['a'], tool_libraries='only')),
            ('older_versions', dict(older_versions='archive')),
            ('max_designs', dict(max_designs=0)),
            ('max_designs', dict(max_designs=True)),
            ('designs must be', dict(designs=[])),
            ('folders must', dict(folders=[{'project': 'project-1'}])),
            ('include_references must', dict(include_references='yes')),
            ('requester', dict(requester='x' * 201)),
            ('hub_timeout_seconds', dict(hub_timeout_seconds=0)),
        ]
        for number, (words, fields) in enumerate(cases):
            self.check_invalid(words, 'job-%d' % number, **dict(fields, id=fields.get('id', 'job-%d' % number)))

    def test_protocol(self):
        self.check_invalid('Protocol 2 is not supported', 'p2', reason='unsupported_protocol', protocol=2)
        path = self.service.jobs / 'p0.json'
        path.write_bytes(json.dumps(dict(id='p0', requested=REQUESTED, output_dir=str(self.output))).encode('utf-8'))
        self.handle()
        self.assertEqual(self.status('p0')['message'], 'protocol is required')

    def test_not_json_or_with_a_byte_order_mark(self):
        (self.service.jobs / 'broken.json').write_bytes(b'{not json')
        (self.service.jobs / 'bom.json').write_bytes(b'\xef\xbb\xbf' + json.dumps(dict(
            protocol=1, id='bom', requested=REQUESTED, output_dir=str(self.output))).encode('utf-8'))
        self.handle()
        self.assertEqual(self.status('broken')['reason'], 'invalid_job')
        self.assertIn('byte-order mark', self.status('bom')['message'])

    def test_own_fields_are_ignored_and_the_job_file_never_changes(self):
        path = self.submit(**{'x-note': {'any': 'thing'}, 'requester': 'Example importer 1.4', 'designs': ['a']})
        before = path.read_bytes()
        self.handle()
        self.assertEqual(self.status()['state'], 'done')
        self.assertEqual(path.read_bytes(), before)

    def test_translation_defaults(self):
        everything = jobs.validate(dict(protocol=1, id='j', requested=REQUESTED, output_dir=str(self.output)), 'j')
        self.assertEqual((everything.config['tool_libraries'], everything.config['designs_only'],
                          everything.config['step'], everything.config['archive']), (True, False, True, True))
        listed = jobs.validate(dict(protocol=1, id='j', requested=REQUESTED, output_dir=str(self.output),
                                    designs=['a'], formats=['step']), 'j')
        self.assertEqual((listed.config['tool_libraries'], listed.config['designs_only'], listed.config['step'],
                          listed.config['archive'], listed.include_references), (False, True, True, False, True))
        folders = jobs.validate(dict(protocol=1, id='j', requested=REQUESTED, output_dir=str(self.output),
                                     folders=[{'project': 'p', 'folder': 'f1'}, {'project': 'p', 'folder': 'f2'}],
                                     max_designs=3, rebuilt_f3z=True), 'j')
        self.assertEqual(folders.config['folders'], {'p': [{'id': 'f1'}, {'id': 'f2'}]})
        self.assertEqual((folders.config['limit'], folders.config['f3z_bundles']), (3, True))


class HeartbeatTests(JobTestCase):
    def test_the_watcher_interval_never_makes_it_late(self):
        # Polled every 3 s, the file is rewritten at most 10 s apart (not 12 s, as seen on Fusion).
        clock = [100.0]
        self.service.clock = lambda: clock[0]
        self.service.write_agent(self.busy)
        writes = []
        original = self.service.write_agent

        def recording(busy=None):
            writes.append(clock[0])
            return original(busy)
        self.service.write_agent = recording
        for _ in range(20):
            clock[0] += 3
            self.service.beat(self.busy, 3)
        gaps = [later - earlier for earlier, later in zip([100.0] + writes, writes)]
        self.assertTrue(gaps)
        self.assertTrue(all(gap <= jobs.HEARTBEAT_SECONDS for gap in gaps), gaps)

    def test_a_running_job_keeps_the_heartbeat_fresh_from_the_main_thread(self):
        # The watcher thread may not run while Fusion exports: the job's own steps beat instead.
        clock = [1000.0]
        self.service.clock = lambda: clock[0]
        self.service.write_agent(self.busy)
        path = self.service.agents / 'fusion-bulk-export.json'
        times = []
        original = jobs.JobHooks.progress

        def progress(hooks, state):
            clock[0] += jobs.HEARTBEAT_SECONDS + 1  # each item takes longer than a heartbeat
            original(hooks, state)
            times.append(json.loads(path.read_bytes())['updated'])
        self.submit()
        with patch.object(jobs.JobHooks, 'progress', progress):
            self.handle()
        self.assertGreater(len(set(times)), 1)


class RunTests(JobTestCase):
    def test_everything_runs_to_done_with_its_status(self):
        states = []
        original = jobs.write_json

        def recording(path, value):
            if str(path).endswith('job-1.status.json'):
                states.append((value['state'], value['progress'].get('current')))
            return original(path, value)
        self.submit()
        with patch('fusion_bulk_export.jobs.write_json', recording):
            self.handle()
        status = self.status()
        self.assertEqual((status['state'], status['reason'], status['exit_code']), ('done', None, 0))
        self.assertEqual(status['counts'], dict(exported=8, up_to_date=0, files=1, files_up_to_date=0, skipped=0,
                                                unsupported=0, failed=0))
        self.assertEqual(status['progress'], dict(done=9, total=9, current=None, current_number=None))
        self.assertEqual(status['message'], 'Exported 8 designs, 0 already up to date, 1 other file saved.')
        self.assertEqual(status['manifest'], str(self.output / 'manifest.json'))
        self.assertEqual(status['result'], str(self.output / 'autorun-result.json'))
        self.assertEqual((status['agent'], status['agent_version'], status['agent_pid']),
                         ('fusion-bulk-export', '9.9.9', os.getpid()))
        self.assertNotIn('inventory', status)
        self.assertEqual(states[0], ('running', None))
        self.assertIn(('running', 'a'), states)  # each item as it goes, by id
        self.assertEqual(states[-1], ('done', None))
        self.assertEqual([state for state, _ in states].count('done'), 1)
        self.assertEqual(self.busy, {'by': None, 'job': None})
        # Final: nothing changes it again.
        before = (self.service.jobs / 'job-1.status.json').read_bytes()
        self.handle()
        self.assertEqual((self.service.jobs / 'job-1.status.json').read_bytes(), before)
        self.assertTrue((self.service.jobs / 'job-1.json').exists())  # the requester deletes it

    def test_agent_says_busy_with_the_job_while_it_runs(self):
        seen = []
        original = jobs.JobHooks.progress

        def progress(hooks, state):
            seen.append(json.loads((self.service.agents / 'fusion-bulk-export.json').read_bytes())['job'])
            return original(hooks, state)
        self.service.write_agent(self.busy)
        self.submit(designs=['a'])
        with patch.object(jobs.JobHooks, 'progress', progress):
            self.handle()
        self.assertEqual(set(seen), {'job-1'})
        agent = json.loads((self.service.agents / 'fusion-bulk-export.json').read_bytes())
        self.assertEqual((agent['busy'], agent['job']), (None, None))

    def test_jobs_run_one_at_a_time_in_requested_order(self):
        order = []
        original = jobs.Service.execute

        def execute(service, app, job, status):
            order.append(job.id)
            return original(service, app, job, status)
        self.submit('b-later', requested='2026-11-23T10:00:00Z', designs=['a'])
        self.submit('a-earlier', requested='2026-11-23T09:00:00+00:00', designs=['b'], output_dir=str(self.home / 'o2'))
        with patch.object(jobs.Service, 'execute', execute):
            self.handle()
        self.assertEqual(order, ['a-earlier', 'b-later'])
        self.assertEqual((self.status('a-earlier')['state'], self.status('b-later')['state']), ('done', 'done'))

    def test_hub_project_and_folder_scopes(self):
        self.submit('hub', hubs=['hub-2'])
        self.submit('project', projects=['project-2'], output_dir=str(self.home / 'p'))
        self.submit('folder', folders=[{'project': 'project-1', 'folder': 'folder-deep'}], output_dir=str(self.home / 'f'))
        self.handle()
        self.assertEqual(self.exported(), ['e'])
        self.assertEqual(self.exported(self.home / 'p'), ['c', 'p3'])
        self.assertEqual(self.exported(self.home / 'f'), ['p2'])
        for stem in ('hub', 'project', 'folder'):
            self.assertEqual(self.status(stem)['state'], 'done', stem)

    def test_ids_not_in_the_account_fail_as_not_found(self):
        self.submit('hub', hubs=['hub-9'])
        self.submit('project', projects=['project-9'], output_dir=str(self.home / 'p'))
        self.submit('folder', folders=[{'project': 'project-1', 'folder': 'folder-9'}], output_dir=str(self.home / 'f'))
        self.submit('design', designs=['a', 'nowhere'], output_dir=str(self.home / 'd'))
        self.handle()
        for stem in ('hub', 'project', 'folder', 'design'):
            status = self.status(stem)
            self.assertEqual((status['state'], status['reason'], status['exit_code']), ('failed', 'not_found', 1), stem)
        self.assertEqual(self.status('design')['message'], '1 of 2 design ids were not found')
        self.assertEqual(self.files['a'].calls, 0)

    def test_design_list_lands_in_the_usual_layout_with_its_linked_designs(self):
        self.submit(designs=['asm', 'b'])
        self.handle()
        status = self.status()
        self.assertEqual(status['state'], 'done')
        self.assertEqual(self.exported(), ['asm', 'b', 'p1', 'p2', 'p3'])  # P2's own link P3 too, each once
        self.assertEqual(status['progress']['total'], 5)
        manifest = self.manifest()
        p2 = next(e for e in manifest['entries'] if e['id'] == 'p2')
        self.assertEqual((p2['hub'], p2['project'], p2['path']), ('Hub', 'Project', ['Parts', 'Deep']))
        self.assertTrue((self.output / 'Hub' / 'Project' / 'Parts' / 'Deep' / 'P2 v3.f3d').is_file())
        self.assertTrue((self.output / 'Hub' / 'Two' / 'P3 v3.f3d').is_file())
        asm = next(e for e in manifest['entries'] if e['id'] == 'asm')
        self.assertEqual(sorted(link['path'] for link in asm['references']),
                         ['Hub/Project/Parts/Deep/P2 v3.f3d', 'Hub/Project/Parts/P1 v3.f3d'])
        self.assertEqual(self.files['c'].calls + self.files['a'].calls + self.files['pdf'].calls, 0)
        self.assertNotIn('tool_libraries', manifest['runs'][-1])  # a design list: not unless asked
        self.assertEqual([self.data.found.count(i) for i in ('asm', 'p1', 'p2', 'p3', 'b')], [1, 1, 1, 1, 1])

    def test_design_list_without_references(self):
        self.submit(designs=['asm'], include_references=False)
        self.handle()
        self.assertEqual(self.exported(), ['asm'])
        self.assertEqual(self.status()['progress']['total'], 1)

    def test_design_list_takes_the_current_version(self):
        old = fake.File('A', 'a', version=2)
        old.latestVersionNumber = 3
        old.latestVersion = self.files['a']
        self.data.findFileById = lambda identity: old if identity == 'a' else None
        self.submit(designs=['a'])
        self.handle()
        self.assertEqual([e['version'] for e in self.manifest()['entries']], [3])

    def test_step_only_job(self):
        self.submit(designs=['b'], formats=['step'])
        self.handle()
        entry = self.manifest()['entries'][0]
        self.assertEqual((entry['archive'], [info['role'] for info in entry['files']]), ('off', ['step']))
        self.assertEqual(self.status()['state'], 'done')

    def test_f3d_only_job_and_options(self):
        self.submit(designs=['asm'], formats=['f3d'], rebuilt_f3z=True, older_versions='keep', max_designs=10)
        self.handle()
        manifest = self.manifest()
        self.assertTrue(all(e['step'] == 'off' for e in manifest['entries']))
        run = manifest['runs'][-1]
        self.assertEqual((run['older_versions'], run['f3z_bundles'], run['step']), ('keep', True, False))
        self.assertIn('Built 2 .f3z files.', self.status()['message'])  # the assembly and P2 (it links P3)

    def test_everything_saves_tool_libraries_by_default(self):
        self.submit(projects=['project-3'])
        self.handle()
        self.assertIn('tool_libraries', self.manifest()['runs'][-1])

    def test_inventory(self):
        self.submit(action='inventory')
        self.handle()
        status = self.status()
        self.assertEqual((status['state'], status['manifest'], status['counts']), ('done', None, None))
        self.assertEqual(status['inventory'], str(self.output / 'inventory.json'))
        self.assertEqual(status['message'], 'Listed 2 hubs, 3 projects, 9 files (8 designs).')
        self.assertTrue(all(file.calls == 0 for file in self.files.values()))

    def test_queued_job_cancelled_or_withdrawn_never_runs(self):
        self.busy['by'] = 'button'
        self.submit('cancelled', designs=['a'])
        path = self.submit('withdrawn', designs=['b'], output_dir=str(self.home / 'o2'))
        self.handle()
        self.assertEqual((self.status('cancelled')['state'], self.status('cancelled')['message']), ('queued', jobs.WAITING))
        (self.service.jobs / 'cancelled.cancel').write_bytes(b'')
        path.unlink()
        self.assertTrue(self.service.wants_attention(self.busy))
        self.handle()
        for stem in ('cancelled', 'withdrawn'):
            status = self.status(stem)
            self.assertEqual((status['state'], status['reason'], status['exit_code']), ('cancelled', 'cancelled', 130))
        self.assertFalse(self.output.exists())

    def test_cancel_while_running_stops_between_items(self):
        original = jobs.JobHooks.progress

        def progress(hooks, state):
            original(hooks, state)
            if state['phase'] == 'design' and state['design'] == 3:
                (self.service.jobs / 'job-1.cancel').write_bytes(b'')
        self.submit(projects=['project-1'])
        with patch.object(jobs.JobHooks, 'progress', progress):
            self.handle()
        status = self.status()
        self.assertEqual((status['state'], status['reason'], status['exit_code']), ('cancelled', 'cancelled', 130))
        self.assertEqual(status['counts']['exported'], 2)
        self.assertEqual(status['progress']['done'], 2)  # the third was not begun
        self.assertEqual(status['message'], 'Canceled after 2 items.')
        self.assertEqual(status['manifest'], str(self.output / 'manifest.json'))
        self.assertTrue(self.manifest()['runs'][-1]['cancelled'])
        # A later job into the same folder carries on.
        self.submit('job-2', projects=['project-1'])
        self.handle()
        self.assertEqual(self.status('job-2')['counts']['up_to_date'], 2)

    def test_waits_while_the_button_export_runs(self):
        self.busy['by'] = 'button'
        self.submit(designs=['a'])
        self.handle()
        self.assertEqual((self.status()['state'], self.status()['message']), ('queued', jobs.WAITING))
        self.assertFalse(self.service.wants_attention(self.busy))  # nothing to do until it ends
        self.busy['by'] = None
        self.assertTrue(self.service.wants_attention(self.busy))
        self.handle()
        self.assertEqual(self.status()['state'], 'done')

    def test_a_folder_in_use_fails_at_once_and_is_not_touched(self):
        import socket
        import time
        self.output.mkdir()
        lock = self.output / 'export.lock'
        lock.write_bytes(json.dumps(dict(pid=os.getppid(), host=socket.gethostname(), started=time.time())).encode())
        self.submit(designs=['a'])
        self.handle()
        status = self.status()
        self.assertEqual((status['state'], status['reason']), ('failed', 'folder_busy'))
        self.assertEqual(sorted(os.listdir(str(self.output))), ['export.lock'])
        # A stale lock (its process gone) is no obstacle.
        lock.write_bytes(json.dumps(dict(pid=dead_pid(), host=socket.gethostname(), started=time.time())).encode())
        self.submit('job-2', designs=['a'])
        self.handle()
        self.assertEqual(self.status('job-2')['state'], 'done')

    def test_an_unusable_output_folder(self):
        self.output.write_bytes(b'a file, not a folder')
        self.submit(designs=['a'])
        self.handle()
        self.assertEqual(self.status()['reason'], 'output_unusable')

    def test_not_signed_in(self):
        self.app.data = fake.with_parents(NS(dataHubs=fake.Collection()))
        self.submit(hub_timeout_seconds=0.01)
        self.handle()
        status = self.status()
        self.assertEqual((status['state'], status['reason']), ('failed', 'not_signed_in'))

    def test_stopped_for_memory_is_exit_75(self):
        from fusion_bulk_export import core
        real = core.Exporter._run

        def stopped(exporter, *args, **kwargs):
            exporter.run_info.update(stopped_code='memory', stopped_reason=core.MEMORY_REASON, discovery_errors=[])
            return core.new_counts()
        self.submit(designs=['a'])
        with patch.object(core.Exporter, '_run', stopped):
            self.handle()
        self.assertTrue(real)
        status = self.status()
        self.assertEqual((status['state'], status['reason'], status['exit_code']), ('failed', 'memory', 75))

    def test_unexpected_error_names_its_type_only(self):
        self.submit(designs=['a'])
        with patch('fusion_bulk_export.autorun.design_list', side_effect=KeyError('a private name')):
            self.handle()
        status = self.status()
        self.assertEqual((status['state'], status['reason'], status['message']),
                         ('failed', 'error', 'Unexpected error: KeyError'))


class RestartTests(JobTestCase):
    def write_status(self, stem, state, pid):
        status = self.service.new_status(stem, state, agent_pid=pid)
        (self.service.jobs / (stem + '.status.json')).write_bytes(json.dumps(status).encode('utf-8'))

    def test_running_job_of_a_gone_fusion_is_interrupted_and_queued_one_taken_over(self):
        gone = dead_pid()
        self.submit('was-running', designs=['a'])
        self.write_status('was-running', 'running', gone)
        self.submit('was-queued', designs=['b'], output_dir=str(self.home / 'o2'))
        self.write_status('was-queued', 'queued', gone)
        self.submit('other-fusion', designs=['c'], output_dir=str(self.home / 'o3'))
        self.write_status('other-fusion', 'running', os.getppid())  # alive: not ours to judge
        self.service.recover(self.busy)
        status = self.status('was-running')
        self.assertEqual((status['state'], status['reason'], status['exit_code']), ('failed', 'interrupted', 77))
        self.assertEqual(self.status('was-queued')['agent_pid'], os.getpid())
        self.assertEqual(self.status('other-fusion')['state'], 'running')
        self.handle()
        self.assertEqual(self.status('was-queued')['state'], 'done')
        self.assertEqual(self.status('was-running')['state'], 'failed')
        self.assertFalse((self.home / 'o3').exists())


class EntryFileTests(JobTestCase):
    """The add-in itself: the service starts with it, the watcher's poll beats and signals jobs, the
    custom event runs them, and stopping removes the agent file."""
    def start_add_in(self):
        import adsk
        adsk.core.CommandEventHandler = object
        adsk.core.CommandCreatedEventHandler = object
        events = {}
        self.fired = []
        self.app.registerCustomEvent = lambda name: events.setdefault(
            name, NS(handlers=[], add=lambda h: events[name].handlers.append(h)))
        self.app.unregisterCustomEvent = lambda name: events.pop(name)
        self.app.fireCustomEvent = lambda name, info: self.fired.append(name)
        definition = NS(commandCreated=NS(add=lambda handler: None), deleteMe=lambda: None)
        self.app.userInterface = NS(
            commandDefinitions=NS(addButtonDefinition=lambda *args: definition),
            allToolbarPanels=NS(itemById=lambda name: NS(controls=NS(addCommand=lambda d: NS(deleteMe=lambda: None)))),
            messageBox=lambda *args: self.fail('no dialog'))
        module = importlib.import_module('FusionBulkExport')
        addin = Path(self.temp.name) / 'addin'
        addin.mkdir()
        for target, value in (('_HERE', addin),):
            patcher = patch.object(module, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.dict(module._jobs, home=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        idle = patch.object(module.Watcher, 'start', lambda watcher: None)
        idle.start()
        self.addCleanup(idle.stop)
        module.run(None)
        self.addCleanup(module.stop, None)
        return module, events

    def test_lifecycle(self):
        agent = self.home / '.cad-export' / 'agents' / 'fusion-bulk-export.json'
        self.submit(designs=['a'])
        stale = dead_pid()
        self.submit('left-running', designs=['b'])
        status = self.service.new_status('left-running', 'running', agent_pid=stale)
        (self.service.jobs / 'left-running.status.json').write_bytes(json.dumps(status).encode('utf-8'))
        module, events = self.start_add_in()
        self.assertTrue(agent.is_file())
        self.assertEqual(json.loads(agent.read_bytes())['fusion_version'], 'test-version')
        self.assertEqual(self.status('left-running')['reason'], 'interrupted')
        self.assertIn('FusionBulkExportJobs', events)
        module._watcher.poll()
        self.assertEqual(self.fired, ['FusionBulkExportJobs'])
        events['FusionBulkExportJobs'].handlers[0].notify(None)
        self.assertEqual(self.status()['state'], 'done')
        self.fired.clear()
        module._watcher.poll()
        self.assertEqual(self.fired, [])  # nothing left to do
        # The heartbeat: the agent file is rewritten once the time has come.
        service = module._jobs['service']
        service.beaten -= jobs.HEARTBEAT_SECONDS + 1
        before = json.loads(agent.read_bytes())['updated']
        import time
        time.sleep(0.002)
        module._watcher.poll()
        self.assertNotEqual(json.loads(agent.read_bytes())['updated'], before)
        module.stop(None)
        self.assertFalse(agent.exists())
        self.assertNotIn('FusionBulkExportJobs', events)

    def test_a_job_waits_while_an_autorun_job_runs(self):
        module, events = self.start_add_in()
        self.submit(designs=['a'])
        module._busy['by'] = 'job'  # an autorun.json job: no protocol job id
        self.addCleanup(module._busy.update, by=None, job=None)
        events['FusionBulkExportJobs'].handlers[0].notify(None)
        self.assertEqual((self.status()['state'], self.status()['message']), ('queued', jobs.WAITING))

    def test_a_broken_service_leaves_the_add_in_working(self):
        with patch('fusion_bulk_export.jobs.Service.setup', side_effect=ValueError('no folder')):
            module, events = self.start_add_in()
        self.assertIsNone(module._jobs['service'])
        self.assertIn('no folder', (module._HERE / 'jobs-error.txt').read_text(encoding='utf-8'))
        self.assertIn('FusionBulkExportAutorun', events)
        module._watcher.poll()


if __name__ == '__main__':
    unittest.main()
