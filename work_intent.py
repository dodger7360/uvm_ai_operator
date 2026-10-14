"""Shared local intent cues, independent of request identity and AI calls."""
import re


def normative_inquiry(text):
    # Editing an actual request takes priority over an informational topic.
    if re.match(r'^\s*(?:исправь|замени|измени|удали|добавь|покажи)\b', text, re.I):
        return False
    topic = re.search(r'гост|стандарт|норматив', text, re.I)
    action = re.search(r'^\s*(?:проверь|проверить|уточни|посмотри|найди|найти|поищи|'
                       r'какие|какой|объясни|расскажи|подскажи|действует|актуален)\b', text, re.I)
    return bool(topic and (action or '?' in text))
