"""Small persisted, typed clarification states; no model call for short answers."""
from decimal import Decimal, InvalidOperation
from typing import Literal
import re
from pydantic import BaseModel, Field


UNITS={'т':'т','тн':'т','тоннах':'т','тонны':'т','кг':'кг','шт':'шт','шт.':'шт','штуках':'шт','штук':'шт','штуки':'шт',
       'м':'м','метрах':'м','м²':'м²','м2':'м²','пог. м':'пог. м','пог.м':'пог. м'}


class ClarificationOption(BaseModel):
    label: str
    values: dict[str,str] = Field(default_factory=dict)


class PendingClarification(BaseModel):
    request_id: str
    item_id: str
    field: Literal['quantity','unit']
    kind: Literal['number','unit','confirmation','choice']
    question: str
    candidate: dict[str,str] = Field(default_factory=dict)
    options: list[ClarificationOption] = Field(default_factory=list)


def find_unit(text):
    compact=text.casefold().strip()
    # Match longest units first, to distinguish metres from square metres.
    for word in sorted(UNITS,key=len,reverse=True):
        if re.search(r'(?<!\w)'+re.escape(word)+r'(?!\w)',compact):
            return UNITS[word]
    return None


def next_question(request):
    for index,item in enumerate(request.items,1):
        if not item.unit:
            choices=[ClarificationOption(label=unit,values={'unit':unit}) for unit in ['т','кг','шт','м','пог. м','м²']]
            return PendingClarification(request_id=request.request_id,item_id=item.item_id,field='unit',kind='choice',
                question=f'Укажите единицу позиции {index}: 1 — т; 2 — кг; 3 — шт.; 4 — м; 5 — пог. м; 6 — м². Можно написать единицу словами.',options=choices)
        if item.quantity is None:
            return PendingClarification(request_id=request.request_id,item_id=item.item_id,field='quantity',kind='number',
                question=f'Укажите количество позиции {index} в {item.unit}.')
    return None


def question_state(question, request):
    """Only associate a model's proposal with an unambiguous existing position."""
    if not question:
        return None
    match=re.search(r'позиц\w*\s*(\d+)',question,re.I)
    index=int(match[1])-1 if match else (0 if len(request.items)==1 else -1)
    if index < 0 or index >= len(request.items):
        return None
    item=request.items[index]
    number=re.search(r'(?<![\w.,])(\d+(?:[.,]\d+)?)\s*(шт\.?|тн|т|кг|м²|пог\.\s*м|м)(?!\w)',question,re.I)
    if re.search(r'верно|подтверд',question,re.I) and number:
        return PendingClarification(request_id=request.request_id,item_id=item.item_id,field='quantity',kind='confirmation',
            question=question,candidate={'quantity':number[1].replace(',','.'),'unit':find_unit(number[2])})
    choices=[]
    for line in question.splitlines():
        option=re.match(r'\s*\d+[).]\s*(.+)',line)
        if option:
            quantity=re.search(r'\d+(?:[.,]\d+)?',option[1])
            unit=find_unit(option[1])
            values={}
            if quantity and unit:
                values['quantity']=quantity[0].replace(',','.')
            if unit:
                values['unit']=unit
            if values:
                choices.append(ClarificationOption(label=option[1],values=values))
    if len(choices)>=2:
        return PendingClarification(request_id=request.request_id,item_id=item.item_id,field='quantity' if 'quantity' in choices[0].values else 'unit',
            kind='choice',question=question,options=choices)
    if re.search(r'количеств|сколько|тоннаж',question,re.I):
        return PendingClarification(request_id=request.request_id,item_id=item.item_id,field='quantity',kind='number',question=question)
    if re.search(r'единиц|в каких',question,re.I):
        return PendingClarification(request_id=request.request_id,item_id=item.item_id,field='unit',kind='unit',question=question)
    return None


def answer_values(pending, text):
    """Return (values, retry-question); never infer quantity from product dimensions."""
    answer=text.casefold().strip().strip('.!?')
    if pending.kind=='confirmation':
        if answer in {'да','верно','подтверждаю','согласен'}:
            return pending.candidate,None
        if answer in {'нет','неверно'}:
            return {},'Укажите правильное количество и единицу.'
        return None,pending.question
    if pending.kind=='choice' and re.fullmatch(r'\d+',answer):
        choice=int(answer)-1
        if 0<=choice<len(pending.options):
            return pending.options[choice].values,None
        return None,pending.question
    unit=find_unit(text)
    if pending.field=='unit':
        return ({'unit':unit},None) if unit else (None,pending.question)
    # A bare number or quantity with unit is valid; a cross-section is not a count.
    match=re.fullmatch(r'\s*(\d+(?:[.,]\d+)?)\s*(?:тн|т|кг|шт\.?|штук|штуки|м²|м2|пог\.\s*м|м)?\s*',text,re.I)
    if match:
        try:
            value=Decimal(match[1].replace(',','.'))
            if value.is_finite() and value>0:
                values={'quantity':str(value)}
                if unit:
                    values['unit']=unit
                return values,None
        except InvalidOperation:
            pass
    return None,pending.question
