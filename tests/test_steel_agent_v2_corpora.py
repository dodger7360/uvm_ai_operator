"""Synthetic corpus checks retain source/count/quantity/technical coverage offline."""
from decimal import Decimal
import pytest
from database import Database
from steel_agent_v2 import SteelAgent, Repository
from steel_agent_v2.engine import totals
from test_steel_agent_v2 import Fake


@pytest.mark.parametrize('kind,count', [('angles',38),('profiles',22),('sheets',27),('beams',10)])
def test_synthetic_corpora_preserve_every_source_and_technical_fact(tmp_path,kind,count):
    source='Синтетический корпус '+kind+'\n'
    records=[dict(id='header',kind='note',start=0,end=len(source)-1)]
    items=[]
    for index in range(1,count+1):
        grade=('С245','Ст3','09Г2С-15')[index%3]
        size=f'{index}×{index+10}×{index%7+1}'
        standard=f'Синтетический норматив {index%4}'
        second_standard=f'Дополнительный норматив {index%3}'
        group=f'Техническое исполнение {index%5}'
        amount=Decimal(index)/Decimal('1000')
        raw=f'{kind} {size} {grade} {standard}; {second_standard}; {group} — {str(amount).replace(".",",")} т'
        start=len(source);source+=raw+'\n'
        identifier=f'r{index}'
        records.append(dict(id=identifier,kind='product',start=start,end=start+len(raw)))
        facts=[dict(name=name,value=value,raw=value,source_ids=[identifier]) for name,value in
               [('raw_grade',grade),('size',size),('group',group),('standard0',standard),('standard1',second_standard)]]
        items.append(dict(id=f'i{index}',source_id=identifier,label=kind,quantity=str(amount),unit='т',
            quantity_text=str(amount).replace('.',','),unit_text='т',characteristics=facts))
    groups=[dict(id=f'g{index}',label='Предложенная группа',item_ids=[item['id'] for item in items[index:index+2]],
                 rationale='Синтетическое предложение GPT') for index in range(0,len(items),2)]
    plan=dict(intent='create_request',extraction=dict(records=records,items=items,groups=groups))
    repository=Repository(Database(tmp_path/'corpus.sqlite3'))
    reply=SteelAgent(Fake(plan),repository).handle(1,source,event_id=kind)
    assert reply.status=='ok'
    snapshot=repository.load(1)
    assert snapshot.original_text==source and len(snapshot.items)==count
    assert [item.model_dump(mode='json') for item in snapshot.items]==items
    assert [item.model_dump(mode='json') for item in snapshot.originals]==items
    assert [record.model_dump() for record in snapshot.records]==records
    assert [group.item_ids for group in snapshot.groups]==[group['item_ids'] for group in groups]
    assert Decimal(totals(snapshot.items)[0]['т'])==sum((Decimal(item['quantity']) for item in items),Decimal(0))
    restarted=Repository(repository.database)
    assert restarted.load(1)==snapshot
    assert restarted.version(1,snapshot.id,1)==snapshot
