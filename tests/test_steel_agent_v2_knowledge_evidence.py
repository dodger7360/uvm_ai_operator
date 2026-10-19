"""Exact evidence checks on isolated copies of owner-approved Git documents."""
import json
from pathlib import Path
import shutil

import pytest

from steel_agent_v2.knowledge import ROOT, PACKAGE, build, section


@pytest.fixture
def source_copy(tmp_path):
    package=json.loads(PACKAGE.read_text(encoding='utf-8'))
    for path in [*package['data']['sources'],'steel_agent_v2/knowledge_manifest.json']:
        target=tmp_path/path;target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(ROOT/path,target)
    return tmp_path


def manifest(root,case,profile,**changes):
    path=root/'steel_agent_v2/knowledge_manifest.json'
    data=json.loads(path.read_text(encoding='utf-8'))
    entry=next(c for c in data['cases'] if c['id'].endswith(case))
    next(r for r in entry['mass_records'] if r['profile']==profile).update(changes)
    path.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')


def document(root,case,number,change):
    path=root/f'docs/steel-agent-v2/knowledge/cases/CASE-20261019-{case}.md'
    original=path.read_text(encoding='utf-8')
    old=section(original,number)
    path.write_text(original.replace(old,change(old),1),encoding='utf-8')


def replace_row(text,profile,change):
    rows=[r for r in text.splitlines() if r.startswith('|') and profile in r]
    assert len(rows)==1
    return text.replace(rows[0],change(rows[0]),1)


@pytest.mark.parametrize('damage',[
    'manifest_substring','cell_substring','neighbor_cell','duplicate_identical',
    'duplicate_other_coefficient','duplicate_disjoint_coefficient','wrong_profile_cell','wrong_header_unit',
    'wrong_cell_unit','wrong_evidence_section','wrong_approved_grade','wrong_source_grade',
])
def test_build_rejects_nonexact_or_ambiguous_evidence(source_copy,damage):
    root=source_copy
    if damage=='manifest_substring':manifest(root,'002','40×40×3',value_text='3')
    elif damage=='cell_substring':
        document(root,'002',8,lambda s:replace_row(s,'40×40×3',lambda r:r.replace('| 3,36 |','| 13,36 |')))
    elif damage=='neighbor_cell':
        document(root,'002',8,lambda s:replace_row(s,'40×40×3',lambda r:r.replace('| 3,36 |','| 12 |')))
    elif damage.startswith('duplicate_'):
        document(root,'002',8,lambda s:replace_row(s,'40×40×3',lambda r:r+'\n'+(
            r if damage=='duplicate_identical' else r.replace('3,36','14,2')
            if damage=='duplicate_disjoint_coefficient' else r.replace('| 3,36 |','| 13,36 |'))))
    elif damage=='wrong_profile_cell':
        document(root,'002',8,lambda s:replace_row(s,'40×40×3',lambda r:r.replace('Труба 40×40×3','Труба 140×40×3')))
    elif damage=='wrong_header_unit':document(root,'002',8,lambda s:s.replace('Принято кг/м','Принято кг/шт'))
    elif damage=='wrong_cell_unit':
        document(root,'006',4,lambda s:replace_row(s,'25К1',lambda r:r.replace('62,6 кг/м','62,6 кг/шт')))
    elif damage=='wrong_evidence_section':manifest(root,'002','40×40×3',evidence_section=7)
    elif damage=='wrong_approved_grade':
        document(root,'002',5,lambda s:s.replace('40×40×3 Ст3','40×40×3 С255'))
    else:
        document(root,'006',4,lambda s:replace_row(s,'30Б1',lambda r:r.replace('30Б1 Ст3,','30Б1 С355,')))
    with pytest.raises(ValueError,match='knowledge_mass_'):
        build(root)


@pytest.mark.parametrize('case,profile,grade',[
    ('002','40×40×3','С255'),('002','50×50×5','С255'),
    ('006','25К1','С355'),('006','30Б1','Ст3'),
    ('006','100×100×4','С255'),('006','18П','С255'),
])
def test_build_rejects_unapproved_manifest_grade(source_copy,case,profile,grade):
    manifest(source_copy,case,profile,grades=[grade])
    with pytest.raises(ValueError,match='knowledge_mass_'):
        build(source_copy)


def test_original_nine_records_and_artifact_are_unchanged(source_copy):
    original=json.loads(PACKAGE.read_text(encoding='utf-8'))
    built=build(source_copy)
    assert built==original
    records=built['data']['mass_records']
    assert len(records)==9
    assert [(r['profile'],r['value'],r['grades']) for r in records]==[
        ('40×40×3','3.36',['Ст3']),('50×50×5','3.77',['Ст3']),
        ('60×40×3','2.23',['Ст3']),('63×40×5','3.91',['Ст3']),
        ('25К1','62.6',['С255']),('30Б1','32.0',['С255']),
        ('60Б2','105.5',['С355']),('18П','16.3',['Ст3']),
        ('100×100×4','11.73',['Ст3']),
    ]


def test_only_approved_beam_has_commercial_grade_change(source_copy):
    records=build(source_copy)['data']['mass_records']
    beam=next(r for r in records if r['profile']=='30Б1')
    assert '| Балка 30Б1 Ст3,' in beam['evidence']
    assert '| Балка 30Б1 С255 ' in beam['evidence']
    assert beam['grades']==['С255'] and beam['family']=='beam' and beam['kind']=='ibeam'
    assert all(r['grades']==['Ст3'] for r in records if r['family'] in {'profile_pipe','angle','channel'})


@pytest.mark.parametrize('case,profile',[
    ('002','40×40×3'),('002','50×50×5'),('006','100×100×4'),('006','18П'),
])
def test_changing_approved_row_and_manifest_cannot_invent_nonbeam_grade_policy(source_copy,case,profile):
    manifest(source_copy,case,profile,grades=['С255'])
    document(source_copy,case,5,lambda s:s.replace(profile+' Ст3',profile+' С255'))
    if case=='006':
        document(source_copy,case,4,lambda s:replace_row(s,profile,lambda r:r.replace(profile+' Ст3 ',profile+' С255 ')))
    with pytest.raises(ValueError,match='knowledge_mass_'):
        build(source_copy)


@pytest.mark.parametrize('old',[
    'Труба 40х40х3 ст3 гост','Уголок 63х40х5 гост ст3',
])
@pytest.mark.parametrize('replacement',['Ст20','Ст3/Ст20','Ст3,Ст20','Ст3 Ст20'])
def test_source_grade_is_its_slot_not_a_later_note(source_copy,old,replacement):
    # Retaining the old grade elsewhere cannot authorize a changed main grade.
    changed=old.replace('ст3',replacement)+' (примечание: ст3)'
    if replacement=='Ст20':
        # Preserve the exact original field ordering, then put the note after quantity.
        changed=old.replace('ст3',replacement)
        def mutate(text):
            rows=text.splitlines()
            return '\n'.join(row.replace(old,changed)+' (примечание: ст3)'
                             if row.startswith(old) else row for row in rows)+'\n'
    else:
        def mutate(text):return text.replace(old,changed)
    document(source_copy,'002',1,mutate)
    with pytest.raises(ValueError,match='knowledge_mass_'):
        build(source_copy)


def test_current_coefficient_source_cannot_be_replaced_by_historical_url(source_copy):
    url='https://metcalc.ru/calc-metalloprokat/steel/balka/table/standard-59'
    document(source_copy,'006',6,lambda text:text.replace(url,'https://example.org/unconfirmed'))
    document(source_copy,'006',7,lambda text:text+'\nИсторическая ссылка: '+url+'\n')
    with pytest.raises(ValueError,match='knowledge_mass_'):
        build(source_copy)


def test_comma_cannot_hide_an_additional_grade_in_approved_result(source_copy):
    document(source_copy,'006',5,lambda text:text.replace('25К1 С255 ','25К1 С255, С355 '))
    document(source_copy,'006',4,lambda text:replace_row(text,'25К1',lambda row:row.replace('25К1 С255 ','25К1 С255, С355 ')))
    with pytest.raises(ValueError,match='knowledge_mass_'):
        build(source_copy)
