"""Explicit opt-in live check; never imports Telegram or opens its working DB.

Run from repo root: python scripts/beta_acceptance.py --live --env /path/to/.env
Keep the persistent ledger and report; default six reserved calls ($0.30), explicitly extendable to eight ($0.40).
"""
import argparse
from decimal import Decimal
import json
import logging
import os
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--interface', choices=['workbench','telegram'], default='telegram')
    parser.add_argument('--resume',action='store_true')
    parser.add_argument('--call-limit',type=int,choices=[6,8],default=6)
    parser.add_argument('--reasoning',choices=['low','medium'],default='low')
    parser.add_argument('--env',type=Path,required=True)
    parser.add_argument('--ledger',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args()
    if not args.live: parser.error('--live is required for paid calls')
    if args.report.exists() and not args.resume:
        parser.error('Existing evidence must be preserved; use --resume with the same ledger')
    from dotenv import load_dotenv
    load_dotenv(args.env,override=False)
    # Fail closed rather than testing another model, provider or higher-price tier.
    if os.getenv('OPENAI_BASE_URL','https://api.openai.com/v1').rstrip('/') != 'https://api.openai.com/v1':
        parser.error('Live check requires the direct OpenAI API endpoint')
    os.environ['OPENAI_MODEL']='gpt-6-luna'
    os.environ['OPENAI_REASONING_EFFORT']=args.reasoning
    os.environ['OPENAI_INPUT_USD_PER_MILLION']='0.10'
    os.environ['OPENAI_CACHED_INPUT_USD_PER_MILLION']='0.01'
    os.environ['OPENAI_OUTPUT_USD_PER_MILLION']='0.50'
    from ai import AIService
    from ai_budget import BetaBudget, budget_actor
    from diagnostics import safe_code
    from workbench import NormalizationWorkbench
    events=[]
    class Capture(logging.Handler):
        def emit(self,record):
            try: events.append(json.loads(record.getMessage()))
            except (TypeError,ValueError): pass
    logger=logging.getLogger('operator_diagnostics');logger.setLevel(logging.INFO);logger.addHandler(Capture())
    ai=AIService();ai.budget=BetaBudget(args.ledger,total_calls=args.call_limit,user_calls=args.call_limit,user_hourly_calls=args.call_limit)
    workbench=NormalizationWorkbench(ai)
    previous=json.loads(args.report.read_text()) if args.resume and args.report.exists() else {}
    results=previous.get('results',[])
    events.extend(previous.get('events',[]))
    for count,unique in [(40,40),(40,20),(40,15),(100,15)]:
        if any(r['source_rows']==count and r['expected_groups']==unique and r['status']=='passed' for r in results):
            continue
        source='\n'.join(f'Уголок {50+i%unique}х{50+i%unique}х5 Ст3 — '
                         + ('1000 кг' if i%2 else '1 т') for i in range(count))
        start=time.monotonic()
        entry={'source_rows':count,'expected_groups':unique,'interface':args.interface,'reasoning':args.reasoning}
        try:
            if args.interface=='workbench':
                with budget_actor(0): result=workbench.process(source)
                groups=result.consolidated_groups
                current=result.current_request
            else:
                import tempfile
                from database import Database
                from repositories.telegram_repository import TelegramRepository
                from telegram_service import TelegramService
                from formatter import request_consolidation
                with tempfile.TemporaryDirectory(prefix='uvm-beta-acceptance-') as folder:
                    repository=TelegramRepository(Database(Path(folder)/'isolated.sqlite3'))
                    service=TelegramService(ai,repository,reliable=True,commercial_view=True)
                    with budget_actor(0): service.process(0,source)
                    current=repository.load(0)
                    assert current is not None and not current.fallback_status, 'fallback_not_acceptance'
                    groups=request_consolidation(current)
            assert len(current.items)==count, 'source_count'
            assert len(groups)==unique, 'group_count'
            assert sorted(i for g in groups for i in g.source_indices)==list(range(1,count+1)), 'coverage'
            assert sum(g.item.quantity for g in groups)==Decimal(count), 'quantity_sum'
            entry.update(status='passed',actual_groups=len(groups))
        except Exception as error:
            entry.update(status='failed',reason=safe_code(error),exception=type(error).__name__)
        entry['duration_seconds']=round(time.monotonic()-start,2)
        results.append(entry)
        report={'model':'gpt-6-luna','results':results,'events':events,
                'max_reserved_usd':str(Decimal(args.call_limit)*Decimal('0.05')),'polling_started':False,'production_database_opened':False}
        args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps(entry,ensure_ascii=False),flush=True)
        if entry['status']!='passed': return 1
    return 0


if __name__=='__main__': raise SystemExit(main())
