"""Explicit offline queue activation marker. Not a startup guard or migration.

The installed protected manifest must already be armed, with both processes stopped.
Does not touch Telegram offset, source data or existing queue jobs.
"""
import argparse
import importlib.util
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from database import Database
from windows_runtime import ProcessLock


def bind(manifest, guard_path, confirmation):
    spec=importlib.util.spec_from_file_location('installed_guard',guard_path)
    guard=importlib.util.module_from_spec(spec);spec.loader.exec_module(guard)
    guard.persistent_manifest(manifest,Path(guard_path).parent)
    m=guard.model(guard.read_json(manifest));guard.bundle_check(m,Path(guard_path).parent)
    guard.require(m['phase']=='armed' and confirmation==f"activate:{m['epoch']}", 'arming_confirmation_required')
    root=Path(m['root'])
    with ProcessLock(root/'telegram-supervisor.lock'),ProcessLock(root/'telegram.lock'):
        guard.require((root/'telegram.maintenance').exists(), 'maintenance_required')
        with guard.read_db(m['legacy_database']) as old,Database(m['v2_database']).connection() as c:
            c.execute('BEGIN IMMEDIATE')
            guard.require(guard.tables(c)==guard.CORE|guard.QUEUE, 'queue_not_migrated')
            meta=dict(c.execute('SELECT key,value FROM steel_v2_queue_meta'))
            guard.require(meta.get('schema')==1 and type(meta.get('cursor')) is int, 'unknown_queue_schema')
            guard.require(guard.offset(old)==meta['cursor'], 'offset_not_aligned')
            guard.require(meta.get('guard_epoch',m['epoch'])==m['epoch'], 'epoch_already_bound')
            guard.require(not c.execute("SELECT 1 FROM steel_v2_inbox WHERE state NOT IN ('delivered','denied','rejected')").fetchone(), 'queue_not_empty_for_activation')
            guard.require(not c.execute('SELECT 1 FROM steel_v2_inflight').fetchone(), 'old_inflight')
            guard.require(not c.execute("SELECT 1 FROM steel_v2_outbox WHERE state NOT IN ('delivered','rejected')").fetchone(), 'pending_outbox')
            c.execute("INSERT OR IGNORE INTO steel_v2_queue_meta VALUES('guard_epoch',?)",(m['epoch'],))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest',type=Path,required=True);p.add_argument('--guard',type=Path,required=True)
    p.add_argument('--confirm',required=True);a=p.parse_args()
    bind(a.manifest,a.guard,a.confirm)
    print('queue_epoch_bound')


if __name__=='__main__':main()
