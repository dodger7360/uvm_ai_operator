import os

from dotenv import load_dotenv
from openai import OpenAI

from schemas import PurchaseRequest


load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SYSTEM_PROMPT = """
Ты извлекаешь позиции из живых заявок на металлопрокат.
Верни только JSON. Используй исходный текст целиком и создай отдельный item
для каждой позиции. Ничего не выдумывай: не добавляй ГОСТ, марку стали,
длину 6/12 м, покрытие или обработку, если их нет в исходном тексте.

Ты только извлекаешь факты, а не определяешь обязательность полей:
missing_fields, clarification_questions и warnings всегда возвращай пустыми.

Используй product_group только из значений: beam, i_beam, channel, angle,
profile_pipe, round_pipe, sheet, expanded_metal, rebar, round_bar, flat_bar.
Сохраняй типоразмер в size_designation.

Правила размеров:
- лист 6×1500×6000: thickness_mm=6, width_mm=1500, length_mm=6000;
- профильная труба 40×40×4: width_mm=40, height_mm=40,
  wall_thickness_mm=4, product_group=profile_pipe;
- круглая труба 57×3,5: diameter_mm=57, wall_thickness_mm=3.5,
  product_group=round_pipe;
- уголок 90×90×6: side_a_mm=90, side_b_mm=90, thickness_mm=6;
- арматура А1 20: grade=А1, diameter_mm=20;
- круг 20: diameter_mm=20;
- полоса 40×4: width_mm=40, thickness_mm=4;
- 25К1, 12П и ПВЛ 506 являются достаточными типоразмерами: сохраняй их в
  size_designation, не придумывай габариты.

Количество и единицу извлекай как указано. source_unit должен хранить исходную
единицу, если она присутствует. source_text содержит исходный фрагмент позиции.
"""

def parse_request(text: str) -> PurchaseRequest:
    """Extracts a draft request. Normalization and validation run separately."""
    response = client.beta.chat.completions.parse(
        model="gpt-5.6-luna",
        response_format=PurchaseRequest,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
    )
    message = response.choices[0].message
    if message.refusal:
        raise ValueError(f"Модель отказалась обработать заявку: {message.refusal}")
    if message.parsed is None:
        raise ValueError("Модель не вернула структурированный ответ.")
    return message.parsed
