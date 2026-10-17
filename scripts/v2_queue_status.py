"""Read-only metadata by default. Explicit offline abandon; never retries or sends."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def inspect(path):
    with sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True) as c:
        states=dict(c.execute('SELECT state,count(*) FROM steel_v2_inbox GROUP BY state'))
        unknown=[dict(update_id=r[0],state=r[1],confirmation=f'{r[0]}:{r[1]}:{r[2]}') for r in c.execute(
            "SELECT update_id,state,input_hash FROM steel_v2_inbox WHERE state IN ('execution_unknown','delivery_unknown') ORDER BY update_id")]
        inflight=c.execute('SELECT count(*) FROM steel_v2_inflight').fetchone()[0]
        outbox=dict(c.execute('SELECT state,count(*) FROM steel_v2_outbox GROUP BY state'))
        cursor=c.execute("SELECT value FROM steel_v2_queue_meta WHERE key='cursor'").fetchone()[0]
        safe=not inflight and all(s in {'delivered','denied','rejected'} for s in states) and all(s in {'delivered','rejected'} for s in outbox)
        return dict(schema=c.execute("SELECT value FROM steel_v2_queue_meta WHERE key='schema'").fetchone()[0],
            states=states,outbox=outbox,inflight=inflight,unknown=unknown,rollback_ready=safe,
            rollback_confirmation='rollback:'+str(cursor))


def prepare_rollback(path, archive, confirmation):
    """Offline, caller holds process lock. Archive ALL tables before removing queue schema.

    Old published v2 Repository refuses extra tables and cannot start until this gate.
    Core history, active state and versions are never deleted or replaced.
    """
    from scripts.v2_sqlite_snapshot import snapshot,metadata
    from steel_agent_v2.queue import TABLES
    from database import Database
    state=inspect(path)
    if state['schema']!=1 or not state['rollback_ready'] or confirmation!=state['rollback_confirmation']:
        raise ValueError('queue_not_safe_for_rollback')
    archive=Path(archive)
    saved=snapshot(Path(path),archive,archive.with_suffix(archive.suffix+'.manifest.json'))
    with Database(path).connection() as c:
        c.execute('BEGIN IMMEDIATE')
        if metadata(c)['tables']!=saved['tables']:raise ValueError('database_changed_after_backup')
        if c.execute("SELECT 1 FROM steel_v2_inbox WHERE state NOT IN ('delivered','denied','rejected')").fetchone():
            raise ValueError('queue_changed_after_backup')
        if c.execute('SELECT 1 FROM steel_v2_inflight').fetchone():raise ValueError('inflight_after_backup')
        if c.execute("SELECT 1 FROM steel_v2_outbox WHERE state NOT IN ('delivered','rejected')").fetchone():raise ValueError('pending_outbox_after_backup')
        for table in TABLES:c.execute('DROP TABLE '+table)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',required=True)
    parser.add_argument('--guard-rollback',action='store_true')
    parser.add_argument('--abandon',type=int)
    parser.add_argument('--prepare-rollback',action='store_true')
    parser.add_argument('--archive',type=Path)
    parser.add_argument('--confirm')
    parser.add_argument('--root',type=Path)
    args=parser.parse_args(argv)
    if args.abandon is not None or args.prepare_rollback:
        if args.root is None or not args.confirm:parser.error('offline --root and exact --confirm are required')
        if args.abandon is not None and args.prepare_rollback:parser.error('choose one offline operation')
        from windows_runtime import ProcessLock
        from database import Database
        from steel_agent_v2.queue import DurableQueue
        from steel_agent_v2.runtime import selection
        handler,database=selection(args.root)
        if handler!='v2' or database!=Path(args.database).resolve():raise ValueError('root_database_mismatch')
        with ProcessLock(args.root/'telegram-supervisor.lock'), ProcessLock(args.root/'telegram.lock'):
            if not (args.root/'telegram.maintenance').exists():raise ValueError('maintenance_required')
            if args.prepare_rollback:
                if args.archive is None:parser.error('--archive required')
                if args.archive.resolve().is_relative_to(args.root.resolve()):raise ValueError('archive_must_be_outside_checkout')
                prepare_rollback(args.database,args.archive,args.confirm)
                print(json.dumps({'rollback_prepared':True,'archive':str(args.archive)}))
                return 0
            DurableQueue(Database(args.database)).resolve(args.abandon,args.confirm)
    result=inspect(args.database)
    print(json.dumps(result,ensure_ascii=False))
    return 2 if args.guard_rollback and not result['rollback_ready'] else 0


if __name__=='__main__':raise SystemExit(main())
