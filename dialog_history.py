"""Bounded complete conversational turns; independent of transport/storage."""
MAX_MESSAGES = 20
MAX_CHARACTERS = 12000
MAX_TURN_CHARACTERS = 8000


def select_turns(newest_first):
    selected, size, count = [], 0, 0
    for turn in newest_first:
        length = sum(len(message['content']) for message in turn)
        if (length > MAX_TURN_CHARACTERS or size+length > MAX_CHARACTERS
                or count+len(turn) > MAX_MESSAGES):
            continue
        selected.append(turn)
        size += length
        count += len(turn)
        if count == MAX_MESSAGES:
            break
    return [message for turn in reversed(selected) for message in turn]


def bounded_history(history):
    turns, index = [], 0
    while index < len(history or []):
        message = history[index]
        if message.get('role') == 'user' and isinstance(message.get('content'), str):
            turn = [message]
            if index+1 < len(history) and history[index+1].get('role') == 'assistant':
                if isinstance(history[index+1].get('content'), str): turn.append(history[index+1])
                else:
                    index += 2
                    continue
                index += 1
            turns.append([{'role':m['role'],'content':m['content']} for m in turn])
        index += 1
    return select_turns(reversed(turns))
