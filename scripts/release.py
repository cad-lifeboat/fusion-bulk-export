#!/usr/bin/env python3
"""Prepare a release of Fusion Bulk Export.

  release.py prepare X.Y.Z

1. Sets "version" in FusionBulkExport/FusionBulkExport.manifest to X.Y.Z.
2. Renames the "## Unreleased" section of CHANGELOG.md to "## X.Y.Z".
3. Runs the tests (python -m unittest discover -s tests); on failure both files are restored.
4. Builds dist/FusionBulkExport-X.Y.Z.zip and dist/release-notes-X.Y.Z.md, and beside the zip
   dist/FusionBulkExport-X.Y.Z.zip.sha256 (one line, sha256sum format) and
   dist/FusionBulkExport-release.json ({version, zip_url, sha256, protocol}: the same name in
   every release, so other programs can read the newest one from
   releases/latest/download/FusionBulkExport-release.json; see docs/job-protocol.md).
5. Prints the git and GitHub commands that commit, tag and publish it (all three assets). It
   runs none of them: nothing here touches the network.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import package  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
VERSION = re.compile(r'^\d+\.\d+\.\d+$')
MANIFEST_VERSION = re.compile(r'("version"\s*:\s*")([^"]*)(")')
UNRELEASED = re.compile(r'^## Unreleased[ \t]*$', re.MULTILINE)
NEXT_HEADING = re.compile(r'^## ', re.MULTILINE)
# Where each release's zip is downloaded from (docs/job-protocol.md, "Installing the add-in").
RELEASES = 'https://github.com/cad-lifeboat/fusion-bulk-export/releases/download'
RELEASE_JSON = 'FusionBulkExport-release.json'
# The job protocol the add-in accepts, read from the add-in's own source (one place to change).
JOBS_SOURCE = Path('FusionBulkExport') / 'fusion_bulk_export' / 'jobs.py'
PROTOCOL_LINE = re.compile(r'^PROTOCOL = (\d+)\s*$', re.MULTILINE)


class ReleaseError(Exception):
    pass


def parse(text):
    return tuple(int(part) for part in text.split('.'))


def bump_manifest(text, new):
    """Manifest text with the version replaced (formatting kept). Returns (text, old version)."""
    match = MANIFEST_VERSION.search(text)
    if not match:
        raise ReleaseError('No "version" in the manifest.')
    old = match.group(2)
    if VERSION.match(old) and parse(new) <= parse(old):
        raise ReleaseError('%s is not newer than the current version %s.' % (new, old))
    return text[:match.start(2)] + new + text[match.end(2):], old


def move_unreleased(text, new):
    """Changelog text with "## Unreleased" renamed to "## <new>". Returns (text, the section's notes)."""
    if re.search(r'^## %s[ \t]*$' % re.escape(new), text, re.MULTILINE):
        raise ReleaseError('CHANGELOG.md already has a %s section.' % new)
    match = UNRELEASED.search(text)
    if not match:
        raise ReleaseError('CHANGELOG.md has no "## Unreleased" section.')
    following = NEXT_HEADING.search(text, match.end())
    notes = text[match.end():following.start() if following else len(text)].strip()
    if not notes:
        raise ReleaseError('The "## Unreleased" section of CHANGELOG.md is empty: nothing to release.')
    return text[:match.start()] + '## ' + new + text[match.end():], notes


def run_tests(root):
    return subprocess.call([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests'], cwd=str(root)) == 0


def protocol(root):
    """The job protocol version the add-in in root accepts (jobs.PROTOCOL)."""
    match = PROTOCOL_LINE.search((Path(root) / JOBS_SOURCE).read_text(encoding='utf-8'))
    if not match:
        raise ReleaseError('No "PROTOCOL = <number>" line in %s.' % JOBS_SOURCE.as_posix())
    return int(match.group(1))


def sha256_of(path):
    sha = hashlib.sha256()
    with open(str(path), 'rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            sha.update(chunk)
    return sha.hexdigest()


def release_assets(new, zip_path, root):
    """Write <zip>.sha256 (sha256sum format) and FusionBulkExport-release.json beside the zip.
    Returns (sha256 path, release.json path)."""
    digest = sha256_of(zip_path)
    sha_path = zip_path.with_name(zip_path.name + '.sha256')
    sha_path.write_bytes(('%s  %s\n' % (digest, zip_path.name)).encode('ascii'))
    info = dict(version=new, zip_url='%s/v%s/%s' % (RELEASES, new, zip_path.name), sha256=digest,
                protocol=protocol(root))
    json_path = zip_path.with_name(RELEASE_JSON)
    json_path.write_bytes((json.dumps(info, indent=2) + '\n').encode('utf-8'))
    return sha_path, json_path


def commands(new, zip_path, notes_path, root, assets=()):
    def relative(path):
        return path.relative_to(root).as_posix() if root in path.parents else str(path)
    files = ' '.join('"%s"' % relative(path) for path in [zip_path] + list(assets))
    return [
        'git add FusionBulkExport/FusionBulkExport.manifest CHANGELOG.md',
        'git commit -m "%s"' % new,
        'git tag -a v%s -m "%s"' % (new, new),
        'git push origin HEAD v%s' % new,
        'gh release create v%s %s --title "%s" --notes-file "%s"' % (new, files, new, relative(notes_path)),
    ]


def prepare(new, root=ROOT, out_dir=None, tests=run_tests, echo=print):
    """Bump, move the changelog, test and build. Returns the zip path."""
    if not VERSION.match(new):
        raise ReleaseError('Version must look like 1.2.3, got %r.' % new)
    root = Path(root).resolve()
    out_dir = Path(out_dir) if out_dir else root / 'dist'
    manifest_path = root / package.NAME / (package.NAME + '.manifest')
    changelog_path = root / 'CHANGELOG.md'
    manifest_before = manifest_path.read_text(encoding='utf-8')
    changelog_before = changelog_path.read_text(encoding='utf-8')
    manifest_after, old = bump_manifest(manifest_before, new)
    changelog_after, notes = move_unreleased(changelog_before, new)

    manifest_path.write_text(manifest_after, encoding='utf-8')
    changelog_path.write_text(changelog_after, encoding='utf-8')
    echo('Version %s -> %s; CHANGELOG "Unreleased" is now "%s".' % (old, new, new))
    try:
        echo('Running the tests...')
        if not tests(root):
            raise ReleaseError('Tests failed.')
        zip_path = package.build(out_dir, root)
        assets = release_assets(new, zip_path, root)
    except BaseException:
        manifest_path.write_text(manifest_before, encoding='utf-8')
        changelog_path.write_text(changelog_before, encoding='utf-8')
        echo('Restored the manifest and CHANGELOG.md.')
        raise
    notes_path = out_dir / ('release-notes-%s.md' % new)
    notes_path.write_text(notes + '\n', encoding='utf-8')
    echo('Built %s' % zip_path)
    echo('Checksum: %s' % assets[0])
    echo('Release info: %s' % assets[1])
    echo('Release notes: %s' % notes_path)
    echo('')
    echo('Check the diff (git diff), then publish with:')
    for line in commands(new, zip_path, notes_path, root, assets):
        echo('  ' + line)
    return zip_path


def main(argv=None):
    parser = argparse.ArgumentParser(description='Prepare a Fusion Bulk Export release (no network).')
    sub = parser.add_subparsers(dest='command')
    prep = sub.add_parser('prepare', help='bump the version, move the changelog, test and build the zip')
    prep.add_argument('version', help='new version, e.g. 0.3.0')
    args = parser.parse_args(argv)
    if args.command != 'prepare':
        parser.print_help()
        return 2
    try:
        prepare(args.version)
    except ReleaseError as error:
        print('release: %s' % error, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
