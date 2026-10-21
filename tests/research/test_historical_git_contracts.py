"""Explicit historical evidence; requires archived Git objects, never active by default."""
from pathlib import Path
from decimal import Decimal
from types import ModuleType
import json
import subprocess
import pytest
from database import Database
from steel_agent_v2 import Repository, SteelAgent
from steel_agent_v2.engine import totals
from test_steel_agent_v2 import Fake
ROOT=Path(__file__).resolve().parents[2]
OLD="aa11380dfe1ed646fec2355b22880fdb03b0d706"

@pytest.mark.parametrize('name',['start-telegram.ps1','stop-telegram.ps1','windows_supervisor.py','windows_runtime.py'])
def test_existing_launch_infrastructure_is_exactly_production(name):
    published=subprocess.check_output(['git','show',OLD+':'+name])
    assert (ROOT/name).read_bytes().replace(b'\r\n',b'\n')==published.replace(b'\r\n',b'\n')


def old_repository_class():
    source=subprocess.run(['git','show','aa11380dfe1ed646fec2355b22880fdb03b0d706:steel_agent_v2/repository.py'],
        capture_output=True,check=True,encoding='utf-8').stdout
    module=ModuleType('steel_agent_v2._published_repository');module.__package__='steel_agent_v2'
    exec(compile(source,'published_repository.py','exec'),module.__dict__)
    return module.Repository


def test_published_repository_refuses_current_queue(tmp_path):
    from steel_agent_v2.queue import DurableQueue
    db=Database(tmp_path/"queue.sqlite3")
    Repository(db);DurableQueue(db)
    with pytest.raises(ValueError):old_repository_class()(db)

def test_four_b6_corpora_via_git_objects_only():
    """Consume source/metadata from B6; never import its implementation/expected text."""
    import hashlib,re,tempfile
    ref='510be4e332ea1f8811dc63e9cf04b3996e62fbe9'
    def blob(path):
        p=subprocess.run(['git','show',ref+':tests/fixtures/b6/'+path],capture_output=True)
        assert p.returncode == 0, 'Research suite requires the documented B6 Git objects'
        return p.stdout
    manifest=json.loads(blob('manifest.json'))
    observed=[]
    for name in ['angles','profiles','sheets','beams']:
        source=blob(name+'_source.txt').decode();data=manifest[name]
        assert hashlib.sha256(source.encode()).hexdigest()==data['source_text_sha256']
        records=[];items=[];covered=bytearray(len(source))
        for n,row in enumerate(data['rows'],1):
            start,end=row['source_start'],row['source_end'];fragment=source[start:end]
            records.append(dict(id=f'r{n}',kind='product',start=start,end=end))
            covered[start:end]=b'\x01'*(end-start)
            expected=Decimal(row['quantity'])
            numbers=re.findall(r'(?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:[.,]\d+)?',fragment)
            matching=[s for s in numbers if Decimal(s.replace(',','.').replace(' ','').replace('\u00a0','').replace('\u202f',''))==expected]
            assert matching, 'fixture quantity must have literal evidence'
            facts=[dict(name='raw_grade',value=row['raw_grade'],raw=fragment,source_ids=[f'r{n}'])]
            facts += [dict(name=key,value=row[key],raw=fragment,source_ids=[f'r{n}']) for key in ('size','group')]
            facts += [dict(name='standard'+str(k),value=value,raw=fragment,source_ids=[f'r{n}']) for k,value in enumerate(row['standards'])]
            items.append(dict(id=f'i{n}',source_id=f'r{n}',label='Позиция из корпуса',quantity=str(expected),unit='т',
                quantity_text=matching[-1],unit_text='т',characteristics=facts))
        offset=0
        while offset<len(source):
            if covered[offset] or source[offset].isspace():offset+=1;continue
            end=offset+1
            while end<len(source) and not covered[end]:end+=1
            records.append(dict(id='note'+str(offset),kind='note',start=offset,end=end));offset=end
        # Arbitrary pairs, NOT the legacy desired group counts or steel heuristics.
        groups=[dict(id=f'g{n}',label='Предложенная группа',item_ids=[i['id'] for i in items[n:n+2]],rationale='Подменённое предложение GPT') for n in range(0,len(items),2)]
        p=dict(intent='create_request',extraction=dict(records=records,items=items,groups=groups))
        with tempfile.TemporaryDirectory() as folder:
            repo=Repository(Database(Path(folder)/'b6.sqlite3'))
            result=SteelAgent(Fake(p),repo).handle(1,source,event_id=name)
            assert result.status=='ok'
            state=repo.load(1)
            assert len(state.items)==data['source_count'] and state.original_text==source
            assert Decimal(totals(state.items)[0]['т'])==sum((Decimal(row['quantity']) for row in data['rows']),Decimal(0))
            assert [i.characteristics[0].value for i in state.items]==[row['raw_grade'] for row in data['rows']]
            assert [next(c.value for c in i.characteristics if c.name=='size') for i in state.items]==[row['size'] for row in data['rows']]
            observed.append((len(state.items),Decimal(totals(state.items)[0]['т'])))
    assert observed==[(38,Decimal('205.355')),(22,Decimal('121.900')),(27,Decimal('442.296')),(10,Decimal('476.313'))]
