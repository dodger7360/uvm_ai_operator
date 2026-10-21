"""Future memory metadata only: no database, Telegram or rule application."""
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from nomenclature import Attribute, NomenclatureSpec, Provenance
from normalization_context import (ConfirmedExperience, ContextualInterpretation,
                                   CorrectionRecord, KnowledgeScope,
                                   UserConfirmation, WorkingContext)


SOURCE = dict(source_id='user-input-test', source_type='text')
PROVENANCE = dict(source_id='user-input-test', record_id='item-a', method='user',
                  fragment='В Ст.3 сп.5')
RECEIPT = dict(status='confirmed', actor_id='test-user',
               confirmed_at=datetime(2026, 10, 14, 12, tzinfo=timezone.utc),
               evidence='Явное подтверждение только для тестовой заявки')


def correction_data():
    base = dict(record_id='item-a', source=SOURCE,
                original_description='Уголок 50х50х5 В Ст.3 сп.5 — 0,193 т',
                quantity=dict(state='present', value=dict(amount='0.193', unit='т'),
                              provenance=[PROVENANCE]))
    before = NomenclatureSpec(**base, context=dict(context_id='context-a', request_id='request-a',
                                                   normalization_version='v1'),
                             material=dict(designation=dict(state='present', value='Ст3',
                                                             provenance=[PROVENANCE])))
    after = NomenclatureSpec(**base, context=dict(context_id='context-a', request_id='request-a',
                                                  normalization_version='v2'),
                            material=dict(designation=dict(state='present', value='В Ст.3 сп.5',
                                                            raw_value='В Ст.3 сп.5', normalized_text='ВСт3сп5',
                                                            provenance=[PROVENANCE]),
                                          supply_group=dict(state='present', value='В', provenance=[PROVENANCE]),
                                          steel_grade=dict(state='present', value='Ст3', provenance=[PROVENANCE]),
                                          deoxidation=dict(state='present', value='сп', provenance=[PROVENANCE]),
                                          category=dict(state='present', value='5', provenance=[PROVENANCE])))
    return dict(correction_id='correction-a', context_id='context-a', request_id='request-a',
                before=before, after=after, original_error='Потеря группы поставки, раскисления и индекса',
                reason='Нужно сохранить полное исходное техническое обозначение',
                changed_attributes=['material.designation', 'material.supply_group',
                                    'material.deoxidation', 'material.category'],
                layer='recognized_attribute', scope=dict(kind='request', request_id='request-a'),
                knowledge_source=SOURCE, rule=dict(rule_id='preserve-source-material', version='v1'))


def experience_data(scope=None, confirmation=None):
    return dict(correction_id='correction-a', scope=scope or dict(kind='request', request_id='request-a'),
                layer='recognized_attribute', knowledge_source=SOURCE,
                rule=dict(rule_id='preserve-source-material', version='v1'),
                confirmation=confirmation or RECEIPT)


def test_correction_snapshots_error_reason_versions_scope_and_decimal_roundtrip():
    correction = CorrectionRecord(**correction_data())
    restored = CorrectionRecord.model_validate_json(correction.model_dump_json())
    assert restored == correction
    assert restored.before.material.designation.value == 'Ст3'
    assert restored.after.material.designation.value == 'В Ст.3 сп.5'
    assert restored.after.material.designation.normalized_text == 'ВСт3сп5'
    assert restored.after.material.supply_group.value == 'В'
    assert restored.before.context.normalization_version == 'v1'
    assert restored.after.context.normalization_version == 'v2'
    assert restored.original_error and restored.reason
    assert restored.after.quantity.value.amount == Decimal('0.193')
    assert restored.before.original_description == restored.after.original_description
    assert restored.scope.kind == 'request'
    assert restored.confirmation.status == 'pending'
    assert restored.after.material.designation.provenance[0].method == 'user'


@pytest.mark.parametrize('change', ['scope', 'record', 'context', 'request'])
def test_correction_cannot_silently_target_other_record_or_context(change):
    data = correction_data()
    if change == 'scope':
        data['scope'] = dict(kind='request', request_id='another-request')
    else:
        after = data['after'].model_dump()
        if change == 'record':
            after['record_id'] = 'another-record'
        elif change == 'context':
            after['context']['context_id'] = 'another-context'
        else:
            after['context']['request_id'] = 'another-request'
        data['after'] = after
    with pytest.raises(ValidationError):
        CorrectionRecord(**data)


@pytest.mark.parametrize('scope', [dict(kind='request'), dict(kind='product_type'),
                                  dict(kind='general', request_id='request-a'),
                                  dict(kind='request', request_id='a', product_type='angle')])
def test_scope_is_explicit_and_unambiguous(scope):
    with pytest.raises(ValidationError):
        KnowledgeScope(**scope)


def test_scope_is_required_and_user_edit_is_not_promoted_to_general_rule():
    data = correction_data()
    data.pop('scope')
    with pytest.raises(ValidationError):
        CorrectionRecord(**data)
    data = correction_data() | dict(confirmation=RECEIPT)
    confirmed = CorrectionRecord(**data)
    assert confirmed.scope.kind == 'request'  # Even after confirmation.
    assert ConfirmedExperience(**experience_data()).scope.kind == 'request'


@pytest.mark.parametrize('status', ['pending', 'rejected'])
def test_unconfirmed_or_rejected_experience_cannot_be_reused(status):
    with pytest.raises(ValidationError):
        ConfirmedExperience(**experience_data(confirmation=dict(status=status)))


@pytest.mark.parametrize('receipt', [dict(status='confirmed'),
                                    RECEIPT | dict(evidence=' '),
                                    RECEIPT | dict(actor_id=None),
                                    RECEIPT | dict(confirmed_at=datetime(2026, 10, 14))])
def test_confirmation_requires_actor_time_timezone_and_evidence(receipt):
    with pytest.raises(ValidationError):
        UserConfirmation(**receipt)


def test_new_request_starts_empty_context_without_mutating_old_corrections():
    old = WorkingContext(context_id='context-a', request_id='request-a', normalization_version='v2',
                         corrections=[CorrectionRecord(**correction_data())])
    fresh = WorkingContext(context_id='context-b', request_id='request-b', normalization_version='v1')
    assert fresh.corrections == [] and fresh.confirmed_experience == []
    assert len(old.corrections) == 1
    with pytest.raises(ValidationError):
        WorkingContext(**(fresh.model_dump() | dict(corrections=old.corrections)))
    with pytest.raises(ValidationError):
        WorkingContext(**(fresh.model_dump() | dict(confirmed_experience=[ConfirmedExperience(**experience_data())])))


@pytest.mark.parametrize('scope', [dict(kind='general', conditions={'rule': 'сохранение исходного обозначения'}),
                                  dict(kind='product_type', product_type='angle',
                                       conditions={'execution': 'equal_leg'})])
def test_new_context_can_explicitly_reference_confirmed_scoped_experience(scope):
    experience = ConfirmedExperience(**experience_data(scope=scope))
    fresh = WorkingContext(context_id='context-b', request_id='request-b', normalization_version='v1',
                           rule_set_version='rules-v1', confirmed_experience=[experience])
    restored = WorkingContext.model_validate_json(fresh.model_dump_json())
    assert restored == fresh
    assert restored.corrections == []  # No replay, retrieval or rule execution.
    assert restored.confirmed_experience[0].scope.conditions == scope['conditions']
    assert restored.confirmed_experience[0].rule.version == 'v1'
    assert restored.confirmed_experience[0].knowledge_source.source_id == SOURCE['source_id']


@pytest.mark.parametrize('layer,label', [('commercial_group', 'ст3'), ('analog_candidate', 'С355')])
def test_commercial_group_and_analog_candidate_are_allowed_without_technical_approval(layer, label):
    source = CorrectionRecord(**correction_data()).after
    before = source.model_dump_json()
    view = ContextualInterpretation(source_record_id=source.record_id, layer=layer,
                                   label=Attribute[str](state='present', value=label,
                                                        provenance=[Provenance(**PROVENANCE)]),
                                   scope=KnowledgeScope(kind='request', request_id='request-a'),
                                   reason='Отдельное контекстное предложение для согласования')
    assert view.technical_assessment == 'not_assessed'
    assert ContextualInterpretation.model_validate_json(view.model_dump_json()) == view
    assert source.model_dump_json() == before
    assert source.material.designation.value == 'В Ст.3 сп.5'
    with pytest.raises(ValidationError):
        ContextualInterpretation.model_validate(view.model_dump() | dict(technical_assessment='equivalent'))
