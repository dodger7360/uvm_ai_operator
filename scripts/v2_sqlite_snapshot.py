"""Explicit SQLite backup and metadata only. No default paths or bot imports."""
import argparse
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3


def metadata(connection):
    integrity = [row[0] for row in connection.execute('PRAGMA integrity_check')]
    if integrity != ['ok']:
        raise ValueError('sqlite_integrity_failed')
    tables = {}
    for name, schema in connection.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"):
        quoted = '"' + name.replace('"', '""') + '"'
        rows = []
        for row in connection.execute('SELECT * FROM ' + quoted):
            encoded = json.dumps(list(row), ensure_ascii=True, separators=(',', ':'),
                                 default=lambda blob: {'blob_hex':blob.hex()}).encode()
            rows.append(sha256(encoded).digest())
        tables[name] = {'rows':len(rows), 'sha256':sha256(b''.join(sorted(rows))).hexdigest(),
                        'schema_sha256':sha256((schema or '').encode()).hexdigest()}
    return {'integrity_check':'ok', 'tables':tables}


def snapshot(source, destination, manifest):
    source, destination, manifest = map(lambda p:Path(p).resolve(), (source, destination, manifest))
    if not source.is_file() or len({source,destination,manifest}) != 3:
        raise ValueError('existing_source_and_distinct_paths_required')
    if destination.exists() or manifest.exists():
        raise FileExistsError('backup_outputs_must_be_new')
    destination.parent.mkdir(parents=True, exist_ok=True)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive reservation: never replace an earlier backup, even on a race.
    with destination.open('xb'):
        pass
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as original:
        with closing(sqlite3.connect(str(destination))) as backup:
            original.backup(backup)
            result = metadata(backup)
    result.update(source=str(source), backup=str(destination))
    with manifest.open('x', encoding='utf-8') as output:
        json.dump(result, output, ensure_ascii=False, indent=2)
        output.write('\n')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    args=parser.parse_args()
    result=snapshot(args.source,args.destination,args.manifest)
    print(json.dumps({'integrity_check':result['integrity_check'], 'tables':len(result['tables'])}))


if __name__=='__main__':
    main()
