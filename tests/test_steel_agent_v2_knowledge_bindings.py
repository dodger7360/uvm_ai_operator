"""Current CASE evidence binds a full source URL and the same product length."""
import json

import pytest

from steel_agent_v2.knowledge import PACKAGE, build
from test_steel_agent_v2_knowledge_evidence import source_copy, manifest, document, replace_row


ROWS=[('002','40×40×3'),('002','50×50×5'),('002','60×40×3'),('002','63×40×5'),
      ('006','25К1'),('006','30Б1'),('006','60Б2'),('006','18П'),('006','100×100×4')]


def original_record(root,case,profile):
    recipe=json.loads((root/'steel_agent_v2/knowledge_manifest.json').read_text(encoding='utf-8'))
    entry=next(c for c in recipe['cases'] if c['id'].endswith(case))
    return next(r for r in entry['mass_records'] if r['profile']==profile)


@pytest.mark.parametrize('case,profile',ROWS)
def test_manifest_length_requires_same_approved_product_length(source_copy,case,profile):
    manifest(source_copy,case,profile,piece_length_mm='8000' if case=='002' else '10000')
    with pytest.raises(ValueError,match='knowledge_mass_'):
        build(source_copy)


@pytest.mark.parametrize('case,profile',ROWS)
def test_url_prefix_is_not_a_complete_evidence_link(source_copy,case,profile):
    record=original_record(source_copy,case,profile)
    manifest(source_copy,case,profile,source_url=record['source_url'][:-1])
    with pytest.raises(ValueError,match='knowledge_mass_'):
        build(source_copy)


@pytest.mark.parametrize('case,profile',[('002','40×40×3'),('006','25К1'),('006','60Б2')])
def test_modified_link_query_does_not_confirm_original_url(source_copy,case,profile):
    url=original_record(source_copy,case,profile)['source_url']
    suffix='&variant=unapproved' if '?' in url else '?variant=unapproved'
    document(source_copy,case,8 if case=='002' else 6,lambda text:text.replace(']('+url+')',']('+url+suffix+')'))
    with pytest.raises(ValueError,match='knowledge_mass_'):
        build(source_copy)


@pytest.mark.parametrize('profile,other',[
    ('25К1','60Б2'),('30Б1','60Б2'),('18П','25К1'),('100×100×4','18П'),
])
def test_current_section_link_for_another_product_is_not_evidence(source_copy,profile,other):
    wrong_url=original_record(source_copy,'006',other)['source_url']
    manifest(source_copy,'006',profile,source_url=wrong_url)
    with pytest.raises(ValueError,match='knowledge_mass_'):
        build(source_copy)


@pytest.mark.parametrize('case,profile',ROWS)
def test_matching_table_row_length_cannot_borrow_approved_or_neighbor_length(source_copy,case,profile):
    if case=='002':
        document(source_copy,case,8,lambda text:replace_row(text,profile,lambda row:row.replace('×6м |','×8м |')))
    else:
        document(source_copy,case,4,lambda text:replace_row(text,profile,lambda row:row.replace(', 12м,',', 10м,')))
    with pytest.raises(ValueError,match='knowledge_mass_'):
        build(source_copy)


@pytest.mark.parametrize('case,profile',[('002','40×40×3'),('006','25К1')])
def test_approved_length_must_agree_with_own_evidence_field(source_copy,case,profile):
    old=' 6м —' if case=='002' else ' 12м —'
    new=' 8м —' if case=='002' else ' 10м —'
    document(source_copy,case,5,lambda text:text.replace(profile+' Ст3'+old,profile+' Ст3'+new)
             if case=='002' else text.replace(profile+' С255'+old,profile+' С255'+new))
    if case=='006':
        # Keep the table's normalized row aligned; its own length field still says 12m.
        document(source_copy,case,4,lambda text:replace_row(text,profile,lambda row:row.replace('С255'+old,'С255'+new)))
    with pytest.raises(ValueError,match='knowledge_mass_'):
        build(source_copy)


@pytest.mark.parametrize('damage',['duplicate_approved','ambiguous_length','duplicate_source','wrong_source_coefficient'])
def test_ambiguous_or_conflicting_current_source_binding_is_rejected(source_copy,damage):
    if damage=='duplicate_approved':
        document(source_copy,'002',5,lambda text:text.replace(
            'Труба профильная 40×40×3 Ст3 6м — 430 шт. (≈8,669 т)',
            'Труба профильная 40×40×3 Ст3 6м — 430 шт. (≈8,669 т)\n'
            'Труба профильная 40×40×3 Ст3 8м — 430 шт. (≈8,669 т)'))
    elif damage=='ambiguous_length':
        document(source_copy,'002',8,lambda text:replace_row(text,'40×40×3',lambda row:row.replace('430×6м','430×6м/8м')))
    else:
        def change(text):
            row=next(line for line in text.splitlines() if line.startswith('- Швеллер 18П:'))
            replacement=row+'\n'+row if damage=='duplicate_source' else row.replace('16,3 кг/м','116,3 кг/м')
            return text.replace(row,replacement)
        document(source_copy,'006',6,change)
    with pytest.raises(ValueError,match='knowledge_mass_'):
        build(source_copy)


@pytest.mark.parametrize('case',['002','006'])
def test_same_units_are_compared_as_exact_decimal_lengths(source_copy,case):
    if case=='002':
        document(source_copy,case,5,lambda text:text.replace(' 6м —',' 6000мм —'))
        document(source_copy,case,8,lambda text:text.replace('×6м |','×6000мм |'))
    else:
        document(source_copy,case,5,lambda text:text.replace(' 12м —',' 12000мм —'))
        document(source_copy,case,4,lambda text:text.replace(', 12м,',', 12000мм,').replace(' 12м —',' 12000мм —'))
    built=build(source_copy)
    assert [r['piece_length_mm'] for r in built['data']['mass_records'][:4]]==['6000']*4
    assert [r['piece_length_mm'] for r in built['data']['mass_records'][4:]]==['12000']*5


def test_original_nine_bindings_keep_the_existing_artifact_and_version(source_copy):
    artifact=json.loads(PACKAGE.read_text(encoding='utf-8'))
    built=build(source_copy)
    assert built==artifact
    assert built['version'].startswith('sha256:9a4')
    assert len(built['data']['mass_records'])==9
    assert [r['piece_length_mm'] for r in built['data']['mass_records']]==['6000']*4+['12000']*5
    assert len(built['data']['cases'])==6
    assert sum(c['position_count'] for c in built['data']['cases'])==25
