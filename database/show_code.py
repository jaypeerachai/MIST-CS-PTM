"""Read a saved source file at a fixed repository commit."""

import argparse
import hashlib
import io
from pathlib import Path
import sqlite3
import tarfile
import tokenize

def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def evidence_path(root, relative):
    root = root.resolve()
    path = Path(relative)
    if path.is_absolute() or '..' in path.parts:
        raise ValueError(f'Invalid evidence path: {relative}')
    path = (root / path).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f'Evidence is outside its directory: {relative}')
    return path


def read_source(connection, evidence_root, repository, commit, path):
    row = connection.execute('''
        SELECT f.git_blob_sha, f.content_sha256, f.size_bytes, f.git_mode, a.path,
               a.sha256, a.size_bytes
        FROM files f JOIN snapshots s USING(snapshot_id)
        JOIN repositories r USING(repository_id)
        JOIN snapshots owner ON owner.repository_id = r.repository_id
        JOIN analyses v ON v.snapshot_id = owner.snapshot_id
        JOIN artifacts a USING(analysis_id)
        WHERE r.name = ? AND s.commit_sha = ? AND f.path = ?
          AND a.kind = 'source_objects'
    ''', (repository, commit, path)).fetchall()
    if len(row) != 1:
        raise ValueError('Expected one saved source file and archive for this repository, commit and path')
    blob, digest, size, mode, relative, archive_hash, archive_size = row[0]
    archive_path = evidence_path(evidence_root, relative)
    if archive_path.stat().st_size != archive_size or sha256(archive_path) != archive_hash:
        raise ValueError('Source archive differs from its record')
    with tarfile.open(archive_path, 'r:gz') as archive:
        member = archive.getmember(blob)
        if not member.isfile() or member.size != size:
            raise ValueError('Unexpected source archive entry')
        content = archive.extractfile(member).read()
    actual_blob = hashlib.sha1(b'blob ' + str(len(content)).encode() + b'\0' + content).hexdigest()
    if actual_blob != blob or hashlib.sha256(content).hexdigest() != digest:
        raise ValueError('Source file differs from its record')
    return content, mode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--evidence-root', type=Path, required=True)
    parser.add_argument('--repository', required=True, help='Exact owner/repository name.')
    parser.add_argument('--commit', required=True, help='Full commit SHA from the database.')
    parser.add_argument('--file', required=True, help='Repository-relative path.')
    parser.add_argument('--start', type=int, default=1, help='First line to show, starting at 1.')
    parser.add_argument('--end', type=int, help='Last line to show, inclusive.')
    args = parser.parse_args()
    if args.start < 1 or (args.end is not None and args.end < args.start):
        parser.error('Use a positive line range with end >= start')
    connection = sqlite3.connect(args.database.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        content, mode = read_source(connection, args.evidence_root, args.repository, args.commit, args.file)
    finally:
        connection.close()
    if mode == '120000':
        print('Symbolic link target (not followed): ' + content.decode('utf-8', errors='replace'))
        return
    encoding = 'utf-8'
    if args.file.endswith('.py'):
        encoding, _ = tokenize.detect_encoding(io.BytesIO(content).readline)
    for number, line in enumerate(content.decode(encoding).splitlines(), 1):
        if number >= args.start and (args.end is None or number <= args.end):
            print(f'{number:5}  {line}')


if __name__ == '__main__':
    main()
