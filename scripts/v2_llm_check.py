"""Explicit future live evaluation, not a Telegram runner. NO API without --live.

Example AFTER separate approval:
python scripts/v2_llm_check.py --live --b6-ref origin/feature/normalizer-recovery-b6 --output tmp/steel-v2/live.json
Credentials must already be in the environment; .env and user databases are not read.
"""
import argparse
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def load_cases(inputs,ref):
    cases=[{'name':p.name,'text':p.read_text(encoding='utf-8')} for p in inputs]
    if ref:
        def blob(name):
            return subprocess.check_output(['git','show',ref+':tests/fixtures/b6/'+name],cwd=ROOT)
        manifest=json.loads(blob('manifest.json'))
        for name in ['angles','profiles','sheets','beams']:
            text=blob(name+'_source.txt').decode('utf-8');meta=manifest[name]
            if sha256(text.encode()).hexdigest()!=meta['source_text_sha256']:
                raise ValueError('B6 source digest mismatch')
            cases.append({'name':name,'text':text,'expected_count':meta['source_count'],
                          'expected_tonnes':str(sum((Decimal(r['quantity']) for r in meta['rows']),Decimal(0)))})
    return cases


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--live',action='store_true')
    p.add_argument('--input',action='append',type=Path,default=[])
    p.add_argument('--b6-ref')
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args(argv)
    if not args.live:p.error('Paid API calls require --live and separate user approval')
    cases=load_cases(args.input,args.b6_ref)
    if not cases:p.error('Provide --input or --b6-ref')
    from database import Database
    from diagnostics import operation, safe_code
    from steel_agent_v2 import SteelAgent,Repository
    from steel_agent_v2.engine import totals
    from steel_agent_v2.transport import OpenAITransport
    transport=OpenAITransport.from_environment()
    results=[]
    with tempfile.TemporaryDirectory(prefix='steel-v2-live-') as folder:
        agent=SteelAgent(transport,Repository(Database(Path(folder)/'isolated.sqlite3')))
        for user,case in enumerate(cases,1):
            start=time.monotonic();before=transport.total_calls
            entry={'case':case['name'],'model':transport.model,'source_sha256':sha256(case['text'].encode()).hexdigest()}
            try:
                with operation() as trace:
                    reply=agent.handle(user,case['text'],event_id='live:'+str(user))
                state=agent.repository.load(user)
                amounts,unknown=totals(state.items) if state else ({},[])
                entry.update(status=reply.status,intent=reply.intent,calls=reply.calls,http_attempts=trace.http_attempts,
                    sdk_retries=trace.sdk_retries,source_count=len(state.items) if state else 0,
                    group_count=len(state.groups) if state else 0,totals=amounts,unknown_quantity_ids=unknown,
                    diagnostics=reply.diagnostics,snapshot=state.model_dump(mode='json') if state else None)
                if 'expected_count' in case:
                    entry['source_count_matches']=entry['source_count']==case['expected_count']
                    entry['tonnes_match']=Decimal(amounts.get('т','NaN'))==Decimal(case['expected_tonnes'])
                entry['normalization_quality']='requires_human_comparison_to_original'
                entry['commercial_groups_quality']='requires_human_review_of_members_and_differences'
            except Exception as error:entry.update(status='failed',reason=safe_code(error))
            entry.update(duration_seconds=round(time.monotonic()-start,3),model_calls=[m for m in transport.metrics if m['call_index']>before])
            results.append(entry)
            args.output.parent.mkdir(parents=True,exist_ok=True)
            args.output.write_text(json.dumps({'results':results,'live_quality_not_inferred_from_offline_tests':True},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            print(json.dumps({k:v for k,v in entry.items() if k not in {'snapshot','model_calls'}},ensure_ascii=False),flush=True)
    return 0


if __name__=='__main__':raise SystemExit(main())
