"""Generate an explicit non-secret manifest proposal; never installs or starts.

Use the protected guard's update command separately, elevated and offline, with
the printed SHA256 confirmation. All source DB access here is read-only.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import sys


def generate(a):
    spec=importlib.util.spec_from_file_location('installed_guard',a.guard)
    g=importlib.util.module_from_spec(spec);spec.loader.exec_module(g)
    if a.action=='baseline':
        required=['root','legacy_database','v2_database','guard_python','worker_python','git','sha']
        if any(getattr(a,k,None) is None for k in required):raise ValueError('explicit_baseline_paths_and_sha_required')
        m={k:str(Path(getattr(a,k)).resolve(strict=True)) for k in required if k!='sha'}
        m.update(version=1,bundle={name:g.digest(a.guard.parent/name) for name in g.BUNDLE},
                 phase='baseline',epoch=0,offset_floor=0,approvals=[],rollback=None)
        with g.read_db(m['legacy_database']) as c:m['offset_floor']=g.offset(c)
    else:
        m=g.model(g.read_json(a.manifest))
        if a.action=='arm':
            if m['phase'] not in {'baseline','retired','active'}:raise ValueError('invalid_source_phase')
            if m['phase']!='active':m['epoch']+=1
            with g.read_db(m['legacy_database']) as old,g.read_db(m['v2_database']) as new:
                cursor=g.queue_state(new)['cursor'] if m['phase']=='active' else g.offset(old)
                g.require(cursor==g.offset(old), 'arming_cursor_not_aligned')
            m.update(phase='armed',offset_floor=cursor,rollback=None)
        elif a.action=='activate':
            g.require(m['phase']=='armed', 'armed_phase_required');m['phase']='active'
        elif a.action=='retire':
            g.require(m['phase']=='armed' and a.archive is not None, 'armed_archive_required')
            archive=a.archive.resolve(strict=True)
            with g.read_db(archive) as saved:
                q=g.queue_state(saved);core=g.core_hash(saved)
            g.require(q['drained'] and q['epoch']==m['epoch'] and q['cursor']==m['offset_floor'], 'archive_not_drained')
            m.update(phase='retired',rollback=dict(epoch=m['epoch'],cursor=q['cursor'],archive=str(archive),sha256=g.digest(archive),core=core))
    if a.sha:
        g.require(a.protocol is not None and a.handlers is not None, 'explicit_approval_required')
        m['approvals'].append(dict(sha=a.sha,queue_protocol=a.protocol,handlers=a.handlers))
    g.model(m)
    with a.out.open('x',encoding='utf-8') as f:json.dump(m,f,ensure_ascii=True,indent=2);f.write('\n')
    print('proposal_sha256='+g.digest(a.out))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['baseline','arm','activate','retire','approve'])
    p.add_argument('--guard',type=Path,required=True);p.add_argument('--manifest',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True);p.add_argument('--archive',type=Path)
    for name in ['root','legacy-database','v2-database','guard-python','worker-python','git']:
        p.add_argument('--'+name,type=Path)
    p.add_argument('--sha');p.add_argument('--protocol',type=int,choices=[0,1]);p.add_argument('--handlers',nargs='+',choices=['legacy','v2'])
    generate(p.parse_args())


if __name__=='__main__':main()
