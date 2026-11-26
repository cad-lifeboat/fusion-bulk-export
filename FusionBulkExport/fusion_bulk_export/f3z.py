"""An .f3z-style bundle built beside an assembly's .f3d, from the .f3d files this export wrote.

An assembly's .f3d, as Fusion's API writes it, holds the assembly's own bodies and links to the
designs inserted into it, not their geometry. With the option on (the dialog's "Also build .f3z
for assemblies", the job's "f3z_bundles", the command line's --f3z, or `autorun.py bundle OUT` for
a folder already exported, without Fusion) this module builds one file beside it that holds the
assembly with every design it inserts (and theirs, for nested assemblies):

    <assembly> v<N>.f3d    the file Fusion wrote: never changed, moved or replaced here
    <assembly> v<N>.f3z    built from it and the .f3d files of the designs it inserts

It is laid out as Fusion's own .f3z (File > Export in Fusion), seen on a real one:
- a plain zip, every member Deflate-compressed, in this order: Manifest.json, the .f3d files
  (sorted by member name), DesignDescription.json;
- one .f3d per distinct design (an assembly that inserts a part twice holds it once), named by a
  UUID (uuid5 of the design's cloud id and version, so a rebuild names it the same);
- Manifest.json: {"root": the assembly's member name};
- DesignDescription.json: the graph of designs. designObjects has one object per member with its
  cloud URNs (lineage: the design's id, which is its lineage URN; about: its version URN), the
  member name as relativePath and downloadAs, and its names; each assembly's object lists the
  designs it inserts as one XREF reference with a relationship each. rootIds is the assembly's.

It is NOT byte for byte what Fusion's own .f3z holds: Fusion's re-saves each design inside it
(larger, with an extra RedirectionsStream.dat that maps its links to the member names). Here each
member is the .f3d this export wrote, copied byte for byte, and the links are mapped only in
DesignDescription.json. A reader has to find each inserted design by its lineage URN and version
there (link_target() is the one place that decides which exported .f3d a link means).

The neutronRole of each relationship (the GUID Fusion gives each link inside the assembly) is
read from the assembly's own Manifest.dat when this Python can read it: Fusion compresses it with
Zstandard, which Python reads from 3.14 on (not Fusion's own Python). Without it the
relationships have no neutronRole; the record says so ("roles": false) and a later pass on a
Python that can read it builds the .f3z again with them.

Which designs: those the manifest records. Each design entry's "references" (from the account)
lists the designs it inserts, by id (lineage URN), name and, from this version on, the version it
uses. A link with a version is the exported .f3d of exactly that version (in this folder, or
moved to the Older versions folder); one without, the newest exported. Nothing is built when
anything is missing or not as expected; the entry records why instead (entry["f3z_not_built"]).

The design's manifest entry records the .f3z as entry["f3z"]: role "f3z_bundle", path, size,
SHA-256, modified time, when it was built, and its members (name, design id, version, SHA-256 of
the .f3d copied, and whether the version was the one the link names). It is up to date while its
file is unchanged and every member is still the same exported .f3d; otherwise it is built again.
A previous .f3z replaced that way follows the "Older versions of a design" choice when it is
unchanged since it was built: moved to the Older versions folder as "<assembly> v<N> (<date
built>).f3z" (move), kept beside under that name (keep), or replaced (delete); entry["f3z_older"]
lists them. A .f3z changed since it was built is never written over. When an assembly's older
version is moved or deleted, its .f3z goes with its other files (core.recorded_files).

Standard library only: the command line runs it outside Fusion, on Python 3.9 and newer.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone

from . import checks
from .names import OLDER_FOLDER, long_path
from .words import form, plural

ROLE = 'f3z_bundle'
SUFFIX = '.f3z'
FORMAT = 2                  # what a recorded .f3z was built as; another value is built again
ZSTANDARD = 93              # the zip compression method Fusion uses for Manifest.dat in a .f3d
MANIFEST_DAT = 'Manifest.dat'
FIRST_OBJECT_ID = 1         # DesignDescription.json's designObjects ids count up from this
OSS_PREFIX = 'urn:adsk.objects:os.object:wip.dm.prod/'
LINEAGE_PREFIX = 'urn:adsk.wipprod:dm.lineage:'
VERSION_PREFIX = 'urn:adsk.wipprod:fs.file:vf.'
NAMESPACE = uuid.UUID('8f3c3f26-2f7a-4f43-9a3b-5f1d2f0c6e11')  # this tool's member names (uuid5)
CHUNK = 1 << 20
MEMBER_ATTRIBUTES = 0o100664 << 16  # a plain file, as in Fusion's own .f3z

# Why a .f3z was not built (entry["f3z_not_built"]). Never names a design, project or path: the
# command line prints counts only.
NO_ARCHIVE = 'its .f3d is not recorded in this folder'
ARCHIVE_CHANGED = 'its .f3d changed since it was exported'
NO_LINKS = 'the inserted designs are not known (export it again to record them)'
CHILD_MISSING = 'an inserted design is not in this export'
CHILD_VERSION_MISSING = 'version %s of an inserted design (the one the assembly uses) is not in this export'
OUT_OF_DATE = ('it uses older versions of inserted designs, and this folder does not record which (export it '
               'again to record them)')
CHILD_CHANGED = "an inserted design's .f3d changed since it was exported"
TAKEN = 'another file already has its name'
EDITED = 'the .f3z there changed since it was built, so it is left as it is'
NOT_WRITTEN = 'it could not be written (%s)'

STRING = re.compile(rb'(?:[\x20-\x7e]\x00){1,}')
GUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
LINEAGE_URN = re.compile(r'^urn:adsk\.wipprod:dm\.lineage:\S+$')


class NotBuilt(Exception):
    """The .f3z of an assembly cannot be built; reason says why in plain words."""
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def zstd_readable():
    """Whether this Python's zipfile reads Zstandard members (compression.zstd, Python 3.14)."""
    try:
        import compression.zstd  # noqa: F401
    except ImportError:
        return False
    return getattr(zipfile, 'ZIP_ZSTANDARD', None) == ZSTANDARD


def strings(data):
    """The strings of a Manifest.dat, in order: each is a 32-bit little-endian count of UTF-16
    code units, then the text (ASCII here; other text is skipped)."""
    found = []
    for match in STRING.finditer(data):
        start = match.start()
        if start < 4:
            continue
        count = struct.unpack('<I', data[start - 4:start])[0]
        text = match.group().decode('utf-16-le')
        if 0 < count <= len(text):
            found.append(text[:count])
    return found


def link_roles(data):
    """{lineage URN: neutronRole} from an assembly's Manifest.dat. Seen on real Fusion: each link
    record starts with its role GUID, then the paths of the linked design, then "LineageId",
    "LineageUrn" with the linked design's lineage URN, "toDocDisplayName", "URL"... So the role
    of a link is the last GUID before its "LineageUrn". A link whose role is not found is left
    out (its relationship then has no neutronRole)."""
    tokens = strings(data)
    roles = {}
    for index, token in enumerate(tokens):
        if token != 'LineageUrn' or index + 1 >= len(tokens) or not LINEAGE_URN.match(tokens[index + 1]):
            continue
        for back in range(index - 1, -1, -1):
            if GUID.match(tokens[back]):
                roles.setdefault(tokens[index + 1], tokens[back])
                break
            if tokens[back] == 'LineageUrn':
                break  # the previous link's
    return roles


def read_roles(path):
    """link_roles() of the .f3d at path, or None when its Manifest.dat cannot be read here (this
    Python cannot read Fusion's Zstandard compression, or the archive is not as expected)."""
    try:
        with zipfile.ZipFile(long_path(path)) as archive:
            info = archive.getinfo(MANIFEST_DAT)
            if info.compress_type == ZSTANDARD and not zstd_readable():
                return None
            return link_roles(archive.read(info))
    except (KeyError, NotImplementedError, zipfile.BadZipFile, OSError, EOFError, RuntimeError):
        return None


def identity(value):
    """The cloud id inside a lineage or version URN (or a bare id), for comparing designs:
    "urn:adsk.wipprod:dm.lineage:AbC" and "urn:adsk.wipprod:fs.file:vf.AbC?version=3" are "AbC"."""
    text = str(value or '').split('?', 1)[0]
    text = text.rsplit(':', 1)[-1]
    return text[3:] if text.startswith('vf.') else text


def lineage_urn(design_id):
    text = str(design_id or '')
    return text if text.startswith(LINEAGE_PREFIX) else LINEAGE_PREFIX + identity(text)


def version_urn(design_id, version):
    return '%s%s?version=%s' % (VERSION_PREFIX, identity(design_id), version)


def archive_of(entry):
    """The recorded .f3d of a design entry that is still meant to be in the folder: (relative path,
    its record) for status done, or where an older version's .f3d was moved (status moved); else
    None."""
    status = entry.get('status')
    if status == 'done':
        for info in entry.get('files') or []:
            if checks.file_role(info) == 'archive' and info.get('path'):
                return info['path'], info
    elif status == 'moved':
        for info in entry.get('moved_files') or []:
            if checks.file_role(info) == 'archive' and info.get('moved_to'):
                return info['moved_to'], info
    return None


class Archives:
    """Every exported .f3d of a folder's manifest by design: find(id, version) the one of that
    version (status done, else moved to the Older versions folder), find(id) the newest done."""
    def __init__(self, manifest):
        self.versions = {}
        for entry in manifest.get('entries') or []:
            if not isinstance(entry, dict) or (entry.get('kind') or 'design') != 'design':
                continue
            found = archive_of(entry)
            if found is None or entry.get('id') is None:
                continue
            path, info = found
            self.versions.setdefault(identity(entry['id']), []).append(
                dict(entry=entry, path=path, sha256=info.get('sha256'), bytes=info.get('bytes'),
                     version=entry.get('version'), done=entry.get('status') == 'done'))

    def find(self, key, version=None):
        known = self.versions.get(identity(key)) or []
        if version is None:
            done = [item for item in known if item['done']]
            return max(done, key=lambda item: item['version'] or 0) if done else None
        exact = [item for item in known if item['version'] == version]
        exact.sort(key=lambda item: not item['done'])
        return exact[0] if exact else None


def links_of(entry):
    """The designs an exported design inserts, from its manifest "references": [(id, version or
    None)], each design once. None when they are not known (references not recorded)."""
    references = entry.get('references')
    if references is None:
        return None
    found, seen = [], set()
    for reference in references:
        if isinstance(reference, dict):
            key, version = reference.get('id'), reference.get('version')
        elif isinstance(reference, str):
            key, version = reference, None
        else:
            continue
        if not key or identity(key) == identity(entry.get('id')):
            continue
        version = version if isinstance(version, int) and not isinstance(version, bool) else None
        if (identity(key), version) in seen:
            continue
        seen.add((identity(key), version))
        found.append((str(key), version))
    return found


def link_target(archives, parent, key, version):
    """Which exported .f3d a link of parent (a design entry) to the design key means: the one of
    the version the link names, else (no version recorded) the newest exported, but only when the
    parent's links are not out of date (then the newest is not the one it uses). Raises NotBuilt.
    The one place that matches links to files: change it here if a reader needs otherwise."""
    if version is not None:
        found = archives.find(key, version)
        if found is None:
            raise NotBuilt(CHILD_VERSION_MISSING % version)
        return found
    if parent.get('references_out_of_date'):
        raise NotBuilt(OUT_OF_DATE)
    found = archives.find(key)
    if found is None:
        raise NotBuilt(CHILD_MISSING)
    return found


def member_name(design_id, version):
    """A member's name in the .f3z: a UUID from the design's id and version."""
    return '%s.f3d' % uuid.uuid5(NAMESPACE, '%s?version=%s' % (identity(design_id), version))


def target_path(entry):
    """The .f3z's relative path: the assembly's .f3d path with .f3z for .f3d. Raises NotBuilt."""
    found = archive_of(entry) if entry.get('status') == 'done' else None
    if found is None or not found[0].lower().endswith('.f3d'):
        raise NotBuilt(NO_ARCHIVE)
    return found[0][:-4] + SUFFIX


def iso(value):
    """A Fusion date (Unix seconds, or milliseconds) as Fusion's .f3z writes dates, or None."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds > 1e11:
        seconds /= 1000.0
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S+0000')
    except (OverflowError, OSError, ValueError):
        return None


def now():
    return datetime.now(timezone.utc).isoformat()


def recorded(entry):
    """entry["f3z"] when it records a file, else None."""
    record = entry.get('f3z')
    return record if isinstance(record, dict) and record.get('path') else None


def plan(archives, entry):
    """The members of an assembly's .f3z: [{'name', 'path', 'sha256', 'bytes', 'entry', 'id',
    'version', 'pinned', 'object', 'links': [member, ...]}], the assembly first, one per distinct
    design and version. Raises NotBuilt."""
    found = archive_of(entry)
    if found is None:
        raise NotBuilt(NO_ARCHIVE)
    top_links = links_of(entry)
    if not top_links:
        raise NotBuilt(NO_LINKS)

    def member(item, pinned):
        design = item['entry']
        return dict(name=member_name(design.get('id'), item['version']), path=item['path'], sha256=item['sha256'],
                    bytes=item['bytes'], entry=design, id=str(design.get('id')), version=item['version'],
                    pinned=pinned, object=FIRST_OBJECT_ID + len(order), links=[])
    order = []
    top = member(dict(entry=entry, path=found[0], sha256=found[1].get('sha256'), bytes=found[1].get('bytes'),
                      version=entry.get('version')), True)
    order.append(top)
    members = {(identity(top['id']), top['version']): top}
    queue = [top]
    while queue:
        parent = queue.pop(0)
        for key, version in links_of(parent['entry']) or []:
            item = link_target(archives, parent['entry'], key, version)
            child = members.get((identity(key), item['version']))
            if child is None:
                child = member(item, version is not None)
                members[(identity(key), item['version'])] = child
                order.append(child)
                queue.append(child)
            if child is not top and child not in parent['links']:
                parent['links'].append(child)
    return order


class Builder:
    """Builds (or finds up to date) the .f3z of every assembly of a folder's manifest (each design
    whose newest exported version inserts other designs: checks.inserted_designs). Changes the
    entries in manifest; the caller saves it. older_versions: 'move', 'keep' or 'delete' (what
    happens to a previous .f3z that is replaced). tally (new_tally()) counts what happened."""
    def __init__(self, root, manifest, older_versions='move', log=None):
        self.root = Path(root)
        self.manifest = manifest
        self.older_versions = older_versions if older_versions in ('move', 'keep', 'delete') else 'move'
        self.log = log or (lambda message: None)
        self.archives = Archives(manifest)
        self.tally = new_tally()
        self.roles_readable = zstd_readable()

    def run(self, cancelled=lambda: False, progress=None):
        assemblies = checks.inserted_designs(self.manifest)
        self.tally['assemblies'] = len(assemblies)
        for number, item in enumerate(assemblies, 1):
            if cancelled():
                self.tally['cancelled'] = True
                break
            if progress is not None:
                progress(number, len(assemblies), item['entry'])
            self.one(item['entry'])
        return self.tally

    def one(self, entry):
        """Build one assembly's .f3z unless it is up to date; never raises for the assembly."""
        try:
            if self.current(entry):
                self.tally['up_to_date'] += 1
                entry.pop('f3z_not_built', None)
                return 'up_to_date'
            self.build(entry)
        except NotBuilt as error:
            entry['f3z_not_built'] = error.reason
            self.tally['not_built'] += 1
            self.log('f3z not built %s: %s' % (entry.get('key'), error.reason))
            return 'not_built'
        entry.pop('f3z_not_built', None)
        self.tally['built'] += 1
        if not entry['f3z'].get('roles'):
            self.tally['without_roles'] += 1
        self.log('f3z built %s (%s)' % (entry.get('key'), plural(len(entry['f3z']['members']), 'design')))
        return 'built'

    def current(self, entry):
        """Whether the recorded .f3z is there unchanged and still holds the right .f3d files (and,
        where this Python can read them, the links' neutronRoles)."""
        record = recorded(entry)
        if record is None or record.get('format') != FORMAT:
            return False
        if not record.get('roles') and self.roles_readable:
            return False
        try:
            if record['path'] != target_path(entry):
                return False
        except NotBuilt:
            return False
        if not members_current(self.archives, entry, record):
            return False
        path = self.root / record['path']
        try:
            stat = os.stat(long_path(path))
        except OSError:
            return False
        if stat.st_size != record.get('bytes'):
            return False
        if stat.st_mtime_ns != record.get('mtime_ns'):
            if checks.file_sha256(path) != record.get('sha256'):
                return False
            record['mtime_ns'] = stat.st_mtime_ns
        return True

    def roles(self, members):
        """{member name: {lineage URN: neutronRole}} of the members that insert designs, or None
        when one of their Manifest.dat cannot be read here."""
        found = {}
        for member in members:
            if not member['links']:
                continue
            roles = read_roles(self.root / member['path'])
            if roles is None:
                return None
            found[member['name']] = roles
        return found

    def build(self, entry):
        target_relative = target_path(entry)
        members = plan(self.archives, entry)
        roles = self.roles(members)  # None when a member's Manifest.dat can't be read by this Python
        target = self.root / target_relative
        record = recorded(entry)
        ours = record is not None and record['path'] == target_relative
        there = None  # the SHA-256 of the file at the target now, if any
        if os.path.lexists(long_path(target)):
            if not os.path.isfile(long_path(target)) or os.path.islink(long_path(target)):
                raise NotBuilt(TAKEN)
            there = checks.file_sha256(target)
            if ours and there != record.get('sha256'):
                raise NotBuilt(EDITED)
        try:
            handle, temporary = tempfile.mkstemp(prefix='.f3z-', suffix='.tmp', dir=long_path(target.parent))
            os.close(handle)
        except OSError as error:
            raise NotBuilt(NOT_WRITTEN % (error.strerror or type(error).__name__))
        try:
            self.write(temporary, members, entry, roles)
            sha = checks.file_sha256(temporary)
            if there is not None and there == sha:
                pass  # the same .f3z is there already (ours, or written by a run that stopped)
            elif there is not None and not ours:
                raise NotBuilt(TAKEN)
            else:
                if there is not None:
                    self.set_aside(entry, record, target)
                self.usual_mode(temporary)
                os.replace(temporary, long_path(target))
                self.set_time(target, members[0])
        except OSError as error:
            raise NotBuilt(NOT_WRITTEN % (error.strerror or type(error).__name__))
        finally:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        stat = os.stat(long_path(target))
        entry['f3z'] = dict(role=ROLE, path=target_relative, sha256=checks.file_sha256(target), bytes=stat.st_size,
                            mtime_ns=stat.st_mtime_ns, built=now(), format=FORMAT, roles=roles is not None,
                            members=[dict(name=member['name'], id=member['id'], version=member['version'],
                                          sha256=member['sha256'], pinned=member['pinned']) for member in members])

    @staticmethod
    def usual_mode(path):
        """mkstemp makes the file readable by its owner only; give it the mode a new file normally gets."""
        if os.name == 'nt':
            return
        mask = os.umask(0)
        os.umask(mask)
        try:
            os.chmod(long_path(path), 0o666 & ~mask)
        except OSError:
            pass

    def set_time(self, target, top):
        """The .f3z gets the modified time of the assembly's .f3d (the design's date)."""
        try:
            stat = os.stat(long_path(self.root / top['path']))
            os.utime(long_path(target), ns=(stat.st_atime_ns, stat.st_mtime_ns))
        except (OSError, ValueError, OverflowError):
            pass

    def write(self, path, members, entry, roles):
        """Write the .f3z at path: Manifest.json, every member's .f3d copied byte for byte (and
        checked against its recorded SHA-256 as it is read), then DesignDescription.json."""
        stamp = zip_time(entry.get('date'))

        def info(name):
            item = zipfile.ZipInfo(name, date_time=stamp)
            item.compress_type = zipfile.ZIP_DEFLATED
            item.create_system = 3
            item.external_attr = MEMBER_ATTRIBUTES
            return item
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            archive.writestr(info('Manifest.json'), compact(dict(root=members[0]['name'])))
            for member in sorted(members, key=lambda item: item['name']):
                source = self.root / member['path']
                digest = hashlib.sha256()
                try:
                    stream = open(long_path(source), 'rb')
                except FileNotFoundError:
                    raise NotBuilt(NO_ARCHIVE if member is members[0] else CHILD_MISSING)
                with stream, archive.open(info(member['name']), 'w',
                                          force_zip64=(member['bytes'] or 0) > (1 << 30)) as sink:
                    for chunk in iter(lambda: stream.read(CHUNK), b''):
                        digest.update(chunk)
                        sink.write(chunk)
                if member['sha256'] and digest.hexdigest() != member['sha256']:
                    raise NotBuilt(ARCHIVE_CHANGED if member is members[0] else CHILD_CHANGED)
            archive.writestr(info('DesignDescription.json'), compact(description(members, entry, roles)))

    def set_aside(self, entry, record, target):
        """The previous .f3z (unchanged since it was built) before a new one replaces it, by the
        "Older versions of a design" choice: moved to the Older versions folder or kept beside,
        as "<name> (<date built>).f3z", or simply replaced (delete)."""
        from .toollibs import dated_name, numbered
        older = dict(path=record['path'], sha256=record.get('sha256'), bytes=record.get('bytes'),
                     built=record.get('built'), date=now())
        if self.older_versions == 'delete':
            older['removed'] = older['date']
            self.tally['removed'] += 1
        else:
            name = dated_name(target.name, record.get('built'))
            base = target.with_name(name) if self.older_versions == 'keep' else (
                self.root / OLDER_FOLDER / Path(record['path']).parent / name)
            candidate, number = base, 1
            while os.path.lexists(long_path(candidate)):
                number += 1
                candidate = numbered(base, number)
            os.makedirs(long_path(candidate.parent), exist_ok=True)
            os.replace(long_path(target), long_path(candidate))
            relative = candidate.relative_to(self.root).as_posix()
            if self.older_versions == 'keep':
                older['kept_as'] = relative
                self.tally['kept'] += 1
            else:
                older['moved_to'] = relative
                self.tally['moved'] += 1
        entry.setdefault('f3z_older', []).append(older)
        self.log('f3z of %s replaced; the previous one was %s' % (entry.get('key'), {
            'delete': 'deleted', 'keep': 'kept beside it'}.get(self.older_versions, 'moved to ' + OLDER_FOLDER)))


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def members_current(archives, entry, record):
    """Whether every member of a recorded .f3z is still the same exported .f3d: the assembly's own
    newest, the version a link names, or the newest for a link without one."""
    members = record.get('members') or []
    if not members:
        return False
    top = members[0]
    found = archive_of(entry) if entry.get('status') == 'done' else None
    if found is None or top.get('sha256') != found[1].get('sha256') or str(top.get('id')) != str(entry.get('id')):
        return False
    for member in members[1:]:
        now_there = archives.find(member.get('id'), member.get('version') if member.get('pinned') else None)
        if now_there is None or now_there['sha256'] != member.get('sha256'):
            return False
    return True


def zip_time(value):
    """A zip member's date (the design's date; 1980 at the earliest, as zip needs)."""
    text = iso(value)
    if text is None:
        return (1980, 1, 1, 0, 0, 0)
    moment = datetime.strptime(text[:19], '%Y-%m-%dT%H:%M:%S')
    return max((moment.year, moment.month, moment.day, moment.hour, moment.minute, moment.second),
               (1980, 1, 1, 0, 0, 0))


def description(members, entry, roles=None):
    """DesignDescription.json of a .f3z, in the shape of Fusion's own (see the module's
    description). roles: {member name: {lineage URN: neutronRole}} (Builder.roles), or None."""
    objects = []
    for member in members:
        design = member['entry']
        display = str(design.get('name') or '')
        known = (roles or {}).get(member['name']) or {}
        references = []
        if member['links']:
            relationships = []
            for child in member['links']:
                role = known.get(lineage_urn(child['id']))
                relationships.append(dict(id=child['object'], metadata=dict(neutronRole=role) if role else {}))
            references.append(dict(type='XREF', ids=[child['object'] for child in member['links']],
                                   relationships=relationships))
        objects.append({
            'id': member['object'], 'version': member['version'],
            'about': version_urn(design.get('id'), member['version']),
            'lineage': lineage_urn(design.get('id')),
            'from': OSS_PREFIX + member['name'],
            'relativePath': member['name'], 'downloadAs': member['name'],
            'friendlyName': display, 'displayName': display, 'zipped': False, 'rootFile': '',
            'contentType': 'f3d', 'shareInfo': None, 'references': references,
            'metadata': {'rootFileName': display + '.f3d'},
            'createdAt': iso(design.get('date'))})
    return {'name': 'Autodesk Design Description', 'version': '0.1', 'designDescription': {
        'id': '0', 'name': 'XRef File Design', 'currentVersion': str(entry.get('version')),
        'designGraphs': [{'creationDate': iso(entry.get('date')), 'creatingService': 'wipdm',
                          'rootIds': [members[0]['object']], 'designObjectRefs': [], 'designObjects': objects}]}}


def new_tally():
    """What one pass did: assemblies seen, .f3z built now (without_roles: of them, how many have no
    neutronRoles), up to date, not built, previous ones moved, kept or removed."""
    return dict(assemblies=0, built=0, up_to_date=0, not_built=0, without_roles=0, moved=0, kept=0, removed=0)


def folder_counts(manifest, assemblies=None):
    """The .f3z state of a folder's assemblies, from the manifest only: built (recorded and still
    holding the right .f3d files), stale (recorded, but a member changed since), not_built (the
    last attempt recorded why), missing (none of these), recorded (built, stale or not_built).
    Counts only."""
    assemblies = checks.inserted_designs(manifest) if assemblies is None else assemblies
    archives = Archives(manifest)
    counts = dict(built=0, stale=0, not_built=0, missing=0)
    for item in assemblies:
        entry = item['entry']
        record = recorded(entry)
        if entry.get('f3z_not_built'):
            counts['not_built'] += 1
        elif record is None:
            counts['missing'] += 1
        elif record.get('format') == FORMAT and members_current(archives, entry, record):
            counts['built'] += 1
        else:
            counts['stale'] += 1
    counts['recorded'] = counts['built'] + counts['stale'] + counts['not_built']
    return counts


# The words of the summary, the report and the command line.
OPTION = 'Also build .f3z for assemblies'
HOW = 'run the export again with "%s" checked, or: python3 scripts/autorun.py bundle <folder>' % OPTION


def note(count, counts=None, on=False):
    """The end of the assemblies note (checks.inserted_note): how to build their .f3z, or how many
    were built and could not be. count: assemblies; counts: folder_counts(); on: this run built
    them (the option was on)."""
    counts = counts or {}
    them = 'it' if count == 1 else 'them'
    if not on and not counts.get('recorded'):
        return 'To build an .f3z beside %s, %s.' % ('it' if count == 1 else 'each', HOW)
    sentences = []
    if counts.get('built'):
        sentences.append('Built %s beside %s.' % (plural(counts['built'], '.f3z file'), them))
    if counts.get('not_built'):
        sentences.append('%d could not be built; the report says why.' % counts['not_built'])
    rest = counts.get('stale', 0) + counts.get('missing', 0)
    if rest:
        sentences.append('To build the %d missing or out of date, %s.' % (rest, HOW))
    return ' '.join(sentences)


def tally_lines(tally):
    """A pass's result in words (counts only): for the command line."""
    if not isinstance(tally, dict):
        return []
    parts = ['built %d now' % tally.get('built', 0), '%d already up to date' % tally.get('up_to_date', 0)]
    if tally.get('not_built'):
        parts.append('%d not built (index.html says why)' % tally['not_built'])
    for key, words in (('moved', 'previous %s moved to ' + OLDER_FOLDER), ('kept', 'previous %s kept beside'),
                       ('removed', 'previous %s replaced')):
        if tally.get(key):
            parts.append('%d %s' % (tally[key], words % form(tally[key], 'copy')))
    lines = ['.f3z for %s: %s' % (plural(tally.get('assemblies', 0), 'assembly'), ', '.join(parts))]
    if tally.get('without_roles'):
        lines.append('%s built without the links\' neutronRole: this Python cannot read the compression Fusion '
                     'uses inside the .f3d. Python 3.14 or newer can: python3 scripts/autorun.py bundle <folder> '
                     'builds them again with it.' % plural(tally['without_roles'], '.f3z file'))
    if tally.get('cancelled'):
        lines.append('Stopped before every .f3z was built; run it again to carry on.')
    return lines


def bundle_folder(root, older_versions='move'):
    """`autorun.py bundle OUT`: build the .f3z of every assembly in an export folder, without
    Fusion (pure file work), then save manifest.json and write index.html and index.csv again.
    The folder is locked as an export locks it. Returns the tally (new_tally()). Raises
    lock.FolderBusy while an export uses the folder, FileNotFoundError when it holds no
    manifest.json, ValueError for a manifest this version does not know."""
    from types import SimpleNamespace
    from . import report
    from .core import SCHEMA, atomic_write
    from .journal import Journal
    from .lock import FolderLock
    root = Path(root)
    if not os.path.isfile(long_path(root / 'manifest.json')):
        raise FileNotFoundError('no manifest.json in ' + str(root))
    lock = FolderLock(root)
    lock.acquire()
    try:
        manifest = checks.read_manifest(root)
        if manifest.get('schema') not in (1, SCHEMA):
            raise ValueError('Unsupported manifest schema')

        def log(message):
            try:
                with open(long_path(root / 'export.log'), 'a', encoding='utf-8') as stream:
                    stream.write(now() + ' ' + message.replace('\n', ' ') + '\n')
            except OSError:
                pass
        log('bundle started (older versions: %s)' % older_versions)
        tally = Builder(root, manifest, older_versions, log).run()
        atomic_write(root / 'manifest.json', checks.stamped(manifest))
        Journal(root / 'manifest.journal').clear()
        log('bundle finished ' + json.dumps(dict((key, value) for key, value in tally.items() if key != 'reasons')))
        runs = [run for run in manifest.get('runs') or [] if isinstance(run, dict)]
        report.write(SimpleNamespace(root=root, manifest=manifest, run_info=runs[-1] if runs else {}))
    finally:
        lock.release()
    return tally
