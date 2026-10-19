"""Build/check the reviewed local knowledge artifact; no network or database."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from steel_agent_v2.knowledge import build, KnowledgePackage, PACKAGE


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--check',action='store_true');args=parser.parse_args()
    package=build();KnowledgePackage(package)
    payload=json.dumps(package,ensure_ascii=False,sort_keys=True,indent=2)+'\n'
    if args.check:
        if not PACKAGE.exists() or PACKAGE.read_text(encoding='utf-8')!=payload:
            raise SystemExit('Knowledge artifact is stale; review sources and rebuild before release')
    else:PACKAGE.write_text(payload,encoding='utf-8')
    print(package['version'])
