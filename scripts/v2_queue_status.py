"""Read-only queue health; explicit offline abandon. Forward-only, never replay/drop."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def legacy_offset(path, v2_path):
    if path is None:raise ValueError('legacy_database_required')
    path=Path(path).resolve();other=Path(v2_path).resolve()
    if path==other or (path.exists() and other.exists() and path.samefile(other)):
        raise ValueError('distinct_legacy_database_required')
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as c:
        row=c.execute("SELECT value FROM telegram_state WHERE key='offset'").fetchone()
        value=int(row[0]) if row else 0
    if value<0:raise ValueError('invalid_legacy_offset')
    return value


def inspect(path, legacy_database=None):
    from steel_agent_v2.queue import DurableQueue
    with sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True) as c:
        c.execute('BEGIN')
        version=c.execute("SELECT value FROM steel_v2_queue_meta WHERE key='schema'").fetchone()
        if not version or version[0]!=1:raise ValueError('unsupported_queue_schema')
        DurableQueue._validate_schema(c)
        states=dict(c.execute('SELECT state,count(*) FROM steel_v2_inbox GROUP BY state'))
        unknown=[dict(update_id=r[0],state=r[1],confirmation=f'{r[0]}:{r[1]}:{r[2]}') for r in c.execute(
            "SELECT update_id,state,input_hash FROM steel_v2_inbox WHERE state IN ('execution_unknown','delivery_unknown') ORDER BY update_id")]
        inflight=c.execute('SELECT count(*) FROM steel_v2_inflight').fetchone()[0]
        outbox=dict(c.execute('SELECT state,count(*) FROM steel_v2_outbox GROUP BY state'))
        cursor=c.execute("SELECT value FROM steel_v2_queue_meta WHERE key='cursor'").fetchone()[0]
        drained=not inflight and all(s in {'delivered','denied','rejected'} for s in states) and all(s in {'delivered','rejected'} for s in outbox)
        # This is not a cross-database snapshot. Installation checks require stop.
        offset=legacy_offset(legacy_database,path) if legacy_database is not None else None
        return dict(schema=1,states=states,outbox=outbox,inflight=inflight,unknown=unknown,
            queue_drained=drained,cursor=cursor,offset=offset,offset_verified=offset==cursor,
            forward_compatible=offset is not None and offset<=cursor,
            mirror_recovery_required=offset is not None and offset<cursor)


def prepare_rollback(*args, **kwargs):
    """Refuse old operator integrations without touching any files."""
    raise ValueError('forward_only_rollback_not_supported')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',required=True)
    parser.add_argument('--legacy-database',type=Path)
    parser.add_argument('--check-forward',action='store_true')
    parser.add_argument('--abandon',type=int)
    parser.add_argument('--confirm')
    parser.add_argument('--root',type=Path)
    args=parser.parse_args(argv)
    if args.check_forward and args.legacy_database is None:parser.error('--legacy-database required')
    if args.abandon is not None:
        if args.root is None or not args.confirm:parser.error('offline --root and exact --confirm are required')
        from windows_runtime import ProcessLock
        from database import Database
        from steel_agent_v2.queue import DurableQueue
        from steel_agent_v2.runtime import selection
        handler,database=selection(args.root)
        if handler!='v2' or database!=Path(args.database).resolve():raise ValueError('root_database_mismatch')
        with ProcessLock(args.root/'telegram-supervisor.lock'), ProcessLock(args.root/'telegram.lock'):
            if not (args.root/'telegram.maintenance').exists():raise ValueError('maintenance_required')
            DurableQueue(Database(args.database)).resolve(args.abandon,args.confirm)
    result=inspect(args.database,args.legacy_database)
    print(json.dumps(result,ensure_ascii=False))
    return 2 if args.check_forward and not result['forward_compatible'] else 0


if __name__=='__main__':raise SystemExit(main())
