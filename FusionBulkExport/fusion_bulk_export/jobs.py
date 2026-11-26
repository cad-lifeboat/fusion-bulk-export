"""Job protocol 1 (docs/job-protocol.md): another program on this computer, run by the same person,
asks the running add-in to export, through files in ~/.cad-export (Windows: %USERPROFILE%\\.cad-export):

  agents/<agent id>.json   this add-in announces itself (Agent file, heartbeat about every 10 s)
  jobs/<id>.json           a job, written by the other program; never changed or deleted here
  jobs/<id>.status.json    its state and progress, written only here (claimed exclusively)
  jobs/<id>.cancel         the other program's request to cancel it

Jobs are validated here, turned into the settings autorun.json takes (autorun.check_settings) and
run by the same code (autorun.run_settings), one at a time, in the order they were requested. The
add-in still only reads from the Fusion account; a job can only ask for this add-in's own export
or inventory, into a folder. Status messages hold counts and error types, never names.

Everything here runs on Fusion's main thread (Service.handle, from the add-in's custom event),
except Service.wants_attention and Service.beat, which the add-in's background watcher calls: they
only read the jobs folder and write the agent file, never touching the Fusion API.
"""
import json
import os
from pathlib import Path
import re
from stat import S_ISREG
import time
import uuid
from datetime import datetime, timezone

from . import autorun
from .core import OLDER_VERSIONS, now, was_canceled
from .lock import NAME as LOCK_NAME, FolderLock, process_alive
from .names import absolute_path, output_root_problem
from .words import plural

PROTOCOL = 1
PROTOCOLS = (1,)
AGENT = 'fusion-bulk-export'
FEATURES = ('export', 'inventory', 'designs_by_id', 'include_references', 'hubs', 'step', 'step_only',
            'f3z_bundles', 'tool_libraries', 'older_versions', 'cancel', 'canceled')
FOLDER = '.cad-export'
HEARTBEAT_SECONDS = 10      # the agent file's "updated" is rewritten this often
STATUS_SECONDS = 10         # a running job's status is rewritten at least this often while listing
MAX_JOB_BYTES = 1 << 20     # a larger job file is never read
JOB_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
REQUESTER_LIMIT = 200
CANCELED = 'canceled'
# The state and reason of a canceled job as written up to 0.4.2; still read (a status file left by
# an earlier version is final all the same).
CANCELED_OLD = 'cancelled'
FINAL = ('done', 'failed', CANCELED, CANCELED_OLD)
FRACTION = re.compile(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\.(\d+)(.*)$')
COUNT_KEYS = ('exported', 'up_to_date', 'files', 'files_up_to_date', 'skipped', 'unsupported', 'failed')
FIELDS = ('protocol', 'id', 'requested', 'requester', 'action', 'output_dir', 'designs', 'include_references',
          'hubs', 'projects', 'folders', 'formats', 'rebuilt_f3z', 'older_versions', 'tool_libraries',
          'designs_only', 'max_designs', 'memory_guard', 'hub_timeout_seconds')
FORMATS = ('f3d', 'step')

EXIT_DONE, EXIT_FAILED, EXIT_MEMORY, EXIT_INTERRUPTED, EXIT_CANCELLED = 0, 1, 75, 77, 130
WAITING = 'Waiting: another export is running in Fusion.'
QUEUED = 'Queued.'
# The plain line of each reason a job failed (counts and error types only: never a name or a path).
MESSAGES = {
    'folder_busy': 'Another export is using the output folder; submit the job again later.',
    'output_unusable': 'Fusion cannot create or write the output folder; use another folder.',
    'not_signed_in': 'No hubs appeared in Fusion; is it signed in?',
    'failing': '5 designs in a row failed (offline or signed out?); check sign-in, then submit again.',
    'disk_full': 'The disk is full.',
    'output_missing': 'The output folder went away (disconnected drive?).',
    'output_unwritable': 'The output folder cannot be written.',
    'memory': 'Fusion is using a lot of memory; restart Fusion, then submit again (it carries on).',
    'account_changed': 'A design changed in the account while it was exported; submit again.',
    'interrupted': 'Fusion closed or crashed while the job ran; submit again (it carries on).',
}


class JobError(Exception):
    """A job that cannot run: reason (a status reason code) and a plain message."""
    def __init__(self, reason, message):
        super().__init__(message)
        self.reason, self.message = reason, message


def invalid(message):
    return JobError('invalid_job', message)


def write_json(path, value):
    """Write value as JSON atomically: a dot-named temporary file in the same folder (readers skip
    those), flushed, then renamed over path. Windows can refuse the rename for a moment while a
    reader has the file open: tried a few times."""
    path = Path(path)
    data = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    temporary = path.with_name('.%s.%s.tmp' % (path.name, uuid.uuid4().hex[:12]))
    try:
        with open(str(temporary), 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(5):
            try:
                os.replace(str(temporary), str(path))
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05)
    finally:
        try:
            os.unlink(str(temporary))
        except OSError:
            pass  # renamed: gone already


def claim_json(path, value):
    """Create path holding value only if it does not exist yet: True when this call created it,
    False when it was there already (another add-in claimed the job). The content goes to a
    temporary file first and is hard-linked to path, so path never appears half-written; where the
    file system has no hard links, path is created exclusively (O_CREAT | O_EXCL) and written."""
    path = Path(path)
    data = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    temporary = path.with_name('.%s.%s.tmp' % (path.name, uuid.uuid4().hex[:12]))
    try:
        with open(str(temporary), 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(str(temporary), str(path))
            return True
        except FileExistsError:
            return False
        except (OSError, AttributeError, NotImplementedError):
            pass  # no hard links on this file system (FAT, some network shares)
        try:
            handle = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_BINARY', 0), 0o600)
        except FileExistsError:
            return False
        with os.fdopen(handle, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        return True
    finally:
        try:
            os.unlink(str(temporary))
        except OSError:
            pass


def read_json(path):
    """A JSON object from path, or None when it is missing, unreadable or not an object."""
    try:
        with open(str(path), 'rb') as stream:
            value = json.loads(stream.read().decode('utf-8'))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def parse_time(text):
    """An ISO 8601 time as a UTC timestamp (seconds), or None. A time without an offset is UTC."""
    if not isinstance(text, str) or not text:
        return None
    value = text.strip()
    if value.endswith(('Z', 'z')):
        value = value[:-1] + '+00:00'
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        # Python before 3.11 takes only 3 or 6 digits of a second's fraction: given 6.
        match = FRACTION.match(value)
        if not match:
            return None
        try:
            moment = datetime.fromisoformat(match.group(1) + '.' + (match.group(2) + '000000')[:6] + match.group(3))
        except ValueError:
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def strings(value, name):
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise invalid('%s must be a nonempty list of ids' % name)
    return list(value)


class Job:
    """A valid job: its id, when it was requested (a timestamp, for the order), its action and
    output folder, the autorun settings it stands for (config), and what autorun.json cannot say
    (hubs, designs, include_references)."""
    def __init__(self, stem, job):
        self.id = stem
        self.raw = job
        self.requested = parse_time(job.get('requested'))
        self.requester = job.get('requester', '')
        self.action = job.get('action', 'export')
        self.output = job.get('output_dir')
        self.hubs = job.get('hubs')
        self.designs = None if job.get('designs', 'all') == 'all' else list(job['designs'])
        self.include_references = job.get('include_references', True)
        self.config = None


def validate(job, stem):
    """The Job for a parsed job file named <stem>.json, or JobError (invalid_job naming the field,
    or unsupported_protocol)."""
    if not isinstance(job, dict):
        raise invalid('The job is not a JSON object')
    if 'protocol' not in job:
        raise invalid('protocol is required')
    protocol = job['protocol']
    if not isinstance(protocol, int) or isinstance(protocol, bool):
        raise invalid('protocol must be an integer')
    if protocol not in PROTOCOLS:
        raise JobError('unsupported_protocol', 'Protocol %d is not supported; this add-in accepts protocol %s.'
                       % (protocol, ', '.join(str(p) for p in PROTOCOLS)))
    unknown = sorted(key for key in job if key not in FIELDS and not str(key).startswith('x-'))
    if unknown:
        raise invalid('Unknown field: %s' % str(unknown[0])[:64])
    for name in ('id', 'requested', 'output_dir'):
        if name not in job:
            raise invalid('%s is required' % name)
    if job['id'] != stem:
        raise invalid('id must equal the job file name without .json')
    if parse_time(job['requested']) is None:
        raise invalid('requested must be an ISO 8601 time')
    requester = job.get('requester', '')
    if not isinstance(requester, str) or len(requester) > REQUESTER_LIMIT:
        raise invalid('requester must be text of at most %d characters' % REQUESTER_LIMIT)
    action = job.get('action', 'export')
    if action not in ('export', 'inventory'):
        raise invalid('action must be "export" or "inventory"')
    output = job['output_dir']
    if not isinstance(output, str) or not output or '\x00' in output:
        raise invalid('output_dir must be a path')
    if not (os.path.isabs(output) and Path(output).is_absolute()):
        raise invalid('output_dir must be an absolute path')
    designs = job.get('designs', 'all')
    if designs != 'all':
        strings(designs, 'designs')
        if action == 'inventory':
            raise invalid('designs must be "all" for an inventory')
        for name in ('hubs', 'projects', 'folders'):
            if name in job:
                raise invalid('%s can only be used with designs "all"' % name)
    for name in ('include_references', 'rebuilt_f3z', 'designs_only', 'memory_guard'):
        if name in job and not isinstance(job[name], bool):
            raise invalid('%s must be true or false' % name)
    config = dict(output=output, mode=action)
    if 'hubs' in job:
        strings(job['hubs'], 'hubs')
    if 'projects' in job:
        config['projects'] = strings(job['projects'], 'projects')
    if 'folders' in job:
        folders = job['folders']
        if not isinstance(folders, list) or not folders or not all(
                isinstance(item, dict) and set(item) == {'project', 'folder'}
                and all(isinstance(item[key], str) and item[key] for key in item) for item in folders):
            raise invalid('folders must be a nonempty list of {"project": id, "folder": id}')
        chosen = {}
        for item in folders:
            chosen.setdefault(item['project'], []).append(dict(id=item['folder']))
        config['folders'] = chosen
    formats = job.get('formats', list(FORMATS))
    if (not isinstance(formats, list) or not formats or len(set(map(str, formats))) != len(formats)
            or not all(isinstance(item, str) and item in FORMATS for item in formats)):
        raise invalid('formats must be ["f3d", "step"], ["f3d"] or ["step"]')
    config.update(step='step' in formats, archive='f3d' in formats)
    if job.get('rebuilt_f3z', False) and 'f3d' not in formats:
        raise invalid('rebuilt_f3z needs "f3d" in formats')
    config['f3z_bundles'] = job.get('rebuilt_f3z', False)
    older = job.get('older_versions', 'move')
    if not isinstance(older, str) or older not in OLDER_VERSIONS:
        raise invalid('older_versions must be one of: ' + ', '.join(OLDER_VERSIONS))
    config['older_versions'] = older
    tools = job.get('tool_libraries', designs == 'all')
    if not (isinstance(tools, bool) or tools == 'only'):
        raise invalid('tool_libraries must be true, false or "only"')
    if tools == 'only' and designs != 'all':
        raise invalid('tool_libraries "only" cannot be used with a design list')
    config['tool_libraries'] = tools
    # A design list holds designs only: other files are left out.
    config['designs_only'] = True if designs != 'all' else job.get('designs_only', False)
    if 'max_designs' in job:
        limit = job['max_designs']
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise invalid('max_designs must be a positive integer')
        config['limit'] = limit
    config['memory_guard'] = job.get('memory_guard', True)
    if 'hub_timeout_seconds' in job:
        timeout = job['hub_timeout_seconds']
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 86400:
            raise invalid('hub_timeout_seconds must be a number of seconds between 0 and 86400')
        config['hub_timeout_seconds'] = timeout
    try:
        autorun.check_settings(config)  # the same checks as an autorun.json job
    except ValueError as error:
        raise invalid(str(error))
    result = Job(stem, job)
    result.config = config
    return result


class JobHooks(autorun.Hooks):
    """A protocol job's part in autorun.run_settings: its .cancel file, its status as items go by,
    its hub filter and design list."""
    def __init__(self, service, job, status):
        self.service, self.job, self.status = service, job, status
        self.hubs = job.hubs
        self.designs = job.designs
        self.include_references = job.include_references
        self.total = None
        self.written = service.clock()

    def cancelled(self):
        return self.service.cancel_path(self.job.id).exists()

    def progress(self, state):
        # The heartbeat from the main thread too: while Fusion runs a job's export, the background
        # watcher may not get to run at all (seen on Fusion 2706: the agent file kept the time the
        # job started), so each step of the job keeps it fresh.
        try:
            self.service.beat(dict(by='job', job=self.job.id))
        except Exception:
            pass
        progress = self.status['progress']
        phase = state.get('phase')
        if phase == 'design':
            number = state.get('design') or 0
            progress.update(done=max(number - 1, 0), current=state.get('id'), current_number=number,
                            total=self.total)
            self.status['counts'] = dict((key, (state.get('counts') or {}).get(key, 0)) for key in COUNT_KEYS)
            self.status['message'] = 'Exporting item %d%s.' % (number, ' of %d' % self.total if self.total else '')
        elif self.service.clock() - self.written < STATUS_SECONDS:
            return
        elif phase == 'listing':
            progress.update(total=self.total)
            self.status['message'] = 'Listing the account (%s so far).' % plural(state.get('design') or 0, 'item')
        elif phase == 'tool_libraries':
            self.status['message'] = 'Saving CAM tool libraries.'
        elif phase == 'f3z':
            self.status['message'] = 'Building .f3z files.'
        self.service.update(self.status)
        self.written = self.service.clock()


class Service:
    """The add-in's side of the protocol for one Fusion session: the agent file, claiming and
    queuing jobs, running them one at a time, and their status files."""
    def __init__(self, version, fusion_version, home=None, pid=None, clock=time.monotonic):
        self.home = Path(home) if home is not None else Path.home()
        self.base = self.home / FOLDER
        self.agents = self.base / 'agents'
        self.jobs = self.base / 'jobs'
        self.agent_path = self.agents / (AGENT + '.json')
        self.version = version
        self.fusion_version = fusion_version
        self.pid = os.getpid() if pid is None else pid
        self.clock = clock
        self.started = now()
        self.beaten = None
        self.queued = set()  # ids of the jobs this add-in has queued (read by the watcher thread)

    # ---- folders and the agent file ----------------------------------------------------------
    def setup(self):
        """Create ~/.cad-export, agents and jobs owner-only when missing. Refused (ValueError) when
        they would lie outside the user's home (a link elsewhere)."""
        home = self.home.resolve()
        for folder in (self.base, self.agents, self.jobs):
            try:
                folder.mkdir(mode=0o700)
            except FileExistsError:
                pass
            if not folder.is_dir():
                raise ValueError('%s is not a folder' % folder.name)
            real = folder.resolve()
            if real != home and home not in real.parents:
                raise ValueError('The %s folder is not inside the home folder' % folder.name)

    def agent(self, busy):
        busy = busy or {}
        return dict(protocol=PROTOCOL, protocols=list(PROTOCOLS), agent=AGENT, exporter=AGENT, version=self.version,
                    fusion_version=self.fusion_version, fusion_running=True, pid=self.pid, started=self.started,
                    updated=now(), busy=busy.get('by'), job=busy.get('job'), jobs_dir=str(self.jobs),
                    features=list(FEATURES))

    def write_agent(self, busy=None):
        write_json(self.agent_path, self.agent(busy))
        self.beaten = self.clock()

    def beat(self, busy=None, every=0):
        """The heartbeat: rewrite the agent file when HEARTBEAT_SECONDS have passed (or it is gone).
        every: how often the caller comes back (the watcher's poll interval), so the file is
        rewritten at the latest HEARTBEAT_SECONDS after the last time, not up to one interval later."""
        if (self.beaten is None or self.clock() - self.beaten + every > HEARTBEAT_SECONDS
                or not self.agent_path.is_file()):
            self.write_agent(busy)

    def remove_agent(self):
        try:
            self.agent_path.unlink()
        except OSError:
            pass

    # ---- job files ------------------------------------------------------------------------------
    def job_path(self, stem):
        return self.jobs / (stem + '.json')

    def status_path(self, stem):
        return self.jobs / (stem + '.status.json')

    def cancel_path(self, stem):
        return self.jobs / (stem + '.cancel')

    def names(self):
        try:
            return set(os.listdir(str(self.jobs)))
        except OSError:
            return set()

    @staticmethod
    def job_ids(names):
        """Ids of the job files among names: <id>.json with a valid id, never a status file, a
        temporary file or a dot-named one."""
        found = []
        for name in names:
            if name.startswith('.') or not name.endswith('.json') or name.endswith('.status.json'):
                continue
            stem = name[:-len('.json')]
            if JOB_ID.match(stem) and not stem.endswith(('.status', '.cancel')):
                found.append(stem)
        return sorted(found)

    def read_job(self, stem):
        """(bytes, modified time) of a job file that may be read, or None: a file that is a link or
        not a regular file, larger than MAX_JOB_BYTES, or (where the system says) not owned by
        this user is never read nor claimed."""
        path = self.job_path(stem)
        try:
            info = os.lstat(str(path))
        except OSError:
            return None
        if not S_ISREG(info.st_mode) or info.st_size > MAX_JOB_BYTES:
            return None
        if hasattr(os, 'getuid') and info.st_uid != os.getuid():
            return None
        try:
            handle = os.open(str(path), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
            with os.fdopen(handle, 'rb') as stream:
                data = stream.read(MAX_JOB_BYTES + 1)
        except OSError:
            return None
        if len(data) > MAX_JOB_BYTES:
            return None
        return data, info.st_mtime

    @staticmethod
    def parse(data, stem):
        """The Job in a job file's bytes, or JobError."""
        try:
            text = data.decode('utf-8')
        except UnicodeDecodeError:
            raise invalid('The job file is not UTF-8')
        if text.startswith('﻿'):
            raise invalid('The job file starts with a byte-order mark')
        try:
            job = json.loads(text)
        except ValueError:
            raise invalid('The job file is not valid JSON')
        return validate(job, stem)

    # ---- status files ---------------------------------------------------------------------------
    def new_status(self, stem, state, **fields):
        status = dict(protocol=PROTOCOL, id=stem, agent=AGENT, agent_version=self.version, agent_pid=self.pid,
                      state=state, reason=None, message='', claimed=now(), started=None, updated=now(),
                      finished=None, progress=dict(done=0, total=None, current=None, current_number=None),
                      counts=None, output_dir=None, manifest=None, result=None, exit_code=None)
        status.update(fields)
        return status

    def update(self, status):
        """Write a status (never over a final one already on disk)."""
        on_disk = read_json(self.status_path(status['id']))
        if on_disk is not None and on_disk.get('state') in FINAL:
            return False  # final: it never changes again
        status['updated'] = now()
        write_json(self.status_path(status['id']), status)
        return True

    def finish(self, status, state, reason=None, message='', exit_code=None):
        if exit_code is None:
            exit_code = (EXIT_DONE if state == 'done' else EXIT_CANCELLED if state in (CANCELED, CANCELED_OLD)
                         else EXIT_FAILED)
        status.update(state=state, reason=reason, message=message, finished=now(), exit_code=exit_code)
        status['progress']['current'] = None
        status['progress']['current_number'] = None
        self.queued.discard(status['id'])
        write_json(self.status_path(status['id']), dict(status, updated=now()))

    # ---- claiming, queuing, running ------------------------------------------------------------
    def wants_attention(self, busy=None):
        """For the background watcher (files only): whether the main thread has something to do: a
        job nobody claimed, or a job this add-in queued that can run now, was canceled or withdrawn."""
        names = self.names()
        for stem in self.job_ids(names):
            if stem + '.status.json' not in names:
                return True
        try:
            queued = list(self.queued)
        except RuntimeError:  # changed by the main thread meanwhile: looked at on the next poll
            queued = []
        if queued and (busy or {}).get('by') is None:
            return True
        return any(stem + '.cancel' in names or stem + '.json' not in names for stem in queued)

    def claim_new(self, busy=None):
        """Claim every job nobody has claimed, oldest request first: an invalid one is answered as
        failed at once, a valid one queued."""
        names = self.names()
        found = []
        for stem in self.job_ids(names):
            if stem + '.status.json' in names:
                continue
            read = self.read_job(stem)
            if read is None:
                continue  # never claimed: a link, too large, not this user's, or gone
            data, modified = read
            try:
                job, problem = self.parse(data, stem), None
            except JobError as error:
                job, problem = None, error
            requested = job.requested if job is not None else None
            found.append(((requested if requested is not None else modified, modified, stem), stem, job, problem))
        for _, stem, job, problem in sorted(found, key=lambda item: item[0]):
            if problem is not None:
                status = self.new_status(stem, 'failed', reason=problem.reason, message=problem.message,
                                         finished=now(), exit_code=EXIT_FAILED)
                claim_json(self.status_path(stem), status)
                continue
            status = self.new_status(stem, 'queued', output_dir=str(job.output),
                                     message=WAITING if (busy or {}).get('by') else QUEUED)
            if claim_json(self.status_path(stem), status):
                self.queued.add(stem)

    def queue(self):
        """[(order, id, status)] of the jobs this add-in has queued, oldest request first."""
        names = self.names()
        found = []
        for stem in self.job_ids(names):
            if stem + '.status.json' not in names:
                continue
            status = read_json(self.status_path(stem))
            if not status or status.get('state') != 'queued' or status.get('agent_pid') != self.pid:
                continue
            read = self.read_job(stem)
            requested = modified = 0.0
            if read is not None:
                modified = read[1]
                try:
                    requested = parse_time(json.loads(read[0].decode('utf-8')).get('requested')) or modified
                except (ValueError, AttributeError):
                    requested = modified
            found.append(((requested, modified, stem), stem, status))
        # A queued job whose job file is gone has its status found through the queued set too.
        for stem in list(self.queued):
            if stem + '.json' not in names and stem + '.status.json' in names:
                status = read_json(self.status_path(stem))
                if status and status.get('state') == 'queued':
                    found.append(((0.0, 0.0, stem), stem, status))
        self.queued = set(stem for _, stem, _ in found)
        return sorted(found, key=lambda item: item[0])

    def withdraw(self, stem, status):
        """A queued job that was canceled or whose job file was removed: canceled, never run.
        True when it was."""
        if self.cancel_path(stem).exists():
            self.finish(status, CANCELED, CANCELED, 'Canceled before it ran.')
            return True
        if not self.job_path(stem).exists():
            self.finish(status, CANCELED, CANCELED, 'The job file was removed before it ran.')
            return True
        return False

    def recover(self, busy=None):
        """At start: a job left running by a Fusion that is gone (or by this add-in before it was
        restarted) failed (interrupted); one left queued is taken over and runs in turn."""
        busy = busy or {}
        names = self.names()
        for stem in self.job_ids(names):
            if stem + '.status.json' not in names or busy.get('job') == stem:
                continue
            status = read_json(self.status_path(stem))
            if not status or status.get('state') in FINAL:
                continue
            pid = status.get('agent_pid')
            if pid != self.pid and process_alive(pid):
                continue  # another Fusion's
            if status.get('state') == 'running':
                status.setdefault('progress', dict(done=0, total=None, current=None, current_number=None))
                self.finish(status, 'failed', 'interrupted', MESSAGES['interrupted'], EXIT_INTERRUPTED)
            elif status.get('state') == 'queued':
                status.update(agent_pid=self.pid, agent_version=self.version, agent=AGENT, message=QUEUED)
                write_json(self.status_path(stem), dict(status, updated=now()))
                self.queued.add(stem)

    def handle(self, app, busy):
        """The custom event's work, on Fusion's main thread: claim new jobs, withdraw canceled
        ones, then (unless another export runs) run the queued jobs one at a time. busy is the
        add-in's {'by': None | 'button' | 'job', 'job': protocol job id}; it is set while a job runs,
        so a call while Fusion handles events during the export only claims."""
        self.claim_new(busy)
        for _, stem, status in self.queue():
            self.withdraw(stem, status)
        while busy.get('by') is None:
            waiting = self.queue()
            if not waiting:
                break
            _, stem, status = waiting[0]
            if self.withdraw(stem, status):
                continue
            self.run(app, stem, status, busy)
            self.claim_new(busy)
        if busy.get('by') is not None:
            for _, stem, status in self.queue():
                if status.get('message') != WAITING and busy.get('job') != stem:
                    status['message'] = WAITING
                    self.update(status)

    def run(self, app, stem, status, busy):
        """Run one queued job to its end; its status is final afterward, whatever happens."""
        self.queued.discard(stem)
        busy.update(by='job', job=stem)
        try:
            self.write_agent(busy)
        except OSError:
            pass
        try:
            read = self.read_job(stem)
            if read is None:
                self.finish(status, CANCELED, CANCELED, 'The job file was removed before it ran.')
                return
            try:
                job = self.parse(read[0], stem)
            except JobError as error:
                self.finish(status, 'failed', error.reason, error.message)
                return
            status.update(state='running', started=now(), message='Starting.', output_dir=str(job.output))
            self.update(status)
            self.execute(app, job, status)
        except Exception as error:
            if status.get('state') not in FINAL:
                self.finish(status, 'failed', 'error', 'Unexpected error: %s' % type(error).__name__)
        finally:
            busy.update(by=None, job=None)
            try:
                self.write_agent(busy)
            except OSError:
                pass

    def execute(self, app, job, status):
        """The job's export or inventory, through the code autorun.json jobs use."""
        output = Path(absolute_path(job.output))
        status['output_dir'] = str(output)
        problem = output_root_problem(output)
        if problem:
            self.finish(status, 'failed', 'output_unusable', problem)
            return
        lock = output / LOCK_NAME
        if os.path.lexists(str(lock)) and not FolderLock(output).stale():
            # Never waited for (it may run for hours), and nothing in that folder is touched.
            self.finish(status, 'failed', 'folder_busy', MESSAGES['folder_busy'])
            return
        if autorun.usable_folder(output) is not None:
            self.finish(status, 'failed', 'output_unusable', MESSAGES['output_unusable'])
            return
        hooks = JobHooks(self, job, status)
        result = autorun.run_settings(app, lambda: (output,) + autorun.check_settings(job.config), output, hooks) or {}
        self.conclude(job, status, result, hooks, output)

    def conclude(self, job, status, result, hooks, output):
        """The final status from the run's result."""
        inventory = job.action == 'inventory'
        result_path = output / 'autorun-result.json'
        status['result'] = str(result_path) if result_path.is_file() else None
        counts = result.get('counts') or {}
        found = result.get('found')
        progress = status['progress']
        if not inventory:
            status['counts'] = dict((key, counts.get(key, 0)) for key in COUNT_KEYS)
            if counts:
                progress['done'] = sum(status['counts'].values())  # items finished, whatever the outcome
        reason = result.get('reason')
        stopped = result.get('stopped_code')
        if reason:
            message = (str((result.get('errors') or [{}])[-1].get('error') or '')
                       if reason in ('not_found', 'invalid_job') else MESSAGES.get(reason))
            if not message:
                message = 'Unexpected error: %s' % (result.get('error_type') or 'unknown')
            state, code = 'failed', EXIT_FAILED
        elif stopped:
            reason, message = stopped, MESSAGES.get(stopped) or 'Stopped by itself.'
            state, code = 'failed', EXIT_MEMORY if stopped == 'memory' else EXIT_FAILED
        elif was_canceled(result):
            reason, message, state, code = CANCELED, 'Canceled after %s.' % plural(
                progress.get('done') or 0, 'item'), CANCELED, EXIT_CANCELLED
        else:
            state, code = 'done', EXIT_DONE
            if inventory:
                message = 'Listed %s, %s, %s (%s).' % (
                    plural(counts.get('hubs', 0), 'hub'), plural(counts.get('projects', 0), 'project'),
                    plural(counts.get('files', 0), 'file'), plural(counts.get('designs', 0), 'design'))
            else:
                message = self.done_message(counts, result)
                progress['total'] = hooks.total if hooks.total is not None else found
        if inventory:
            listing = output / 'inventory.json'
            status['inventory'] = str(listing) if state == 'done' and listing.is_file() else None
            status['manifest'] = None
        else:
            manifest = output / 'manifest.json'
            status['manifest'] = str(manifest) if manifest.is_file() else None
        self.finish(status, state, reason, message, code)

    @staticmethod
    def done_message(counts, result):
        parts = ['Exported %s' % plural(counts.get('exported', 0), 'design'),
                 '%d already up to date' % counts.get('up_to_date', 0)]
        if counts.get('files'):
            parts.append('%s saved' % plural(counts['files'], 'other file'))
        if counts.get('failed'):
            parts.append('%d failed' % counts['failed'])
        text = ', '.join(parts) + '.'
        built = (result.get('f3z') or {}).get('built')
        if built:
            text += ' Built %s.' % plural(built, '.f3z file')
        return text
