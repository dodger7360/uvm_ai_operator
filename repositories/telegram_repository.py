"""Additive SQLite storage; existing procurement tables are untouched."""
from telegram_service import ActiveRequest


class TelegramRepository:
    def __init__(self, database):
        self.database = database
        with database.connection() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS telegram_history (
                id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
                request_id TEXT NOT NULL, request_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS telegram_active (
                user_id INTEGER PRIMARY KEY, request_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS telegram_state (
                key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)

    def load(self, user_id):
        with self.database.connection() as c:
            row = c.execute('SELECT request_json FROM telegram_active WHERE user_id=?', (user_id,)).fetchone()
        return ActiveRequest.model_validate_json(row[0]) if row else None

    def save(self, user_id, request):
        data = request.model_dump_json()
        with self.database.connection() as c:
            c.execute('INSERT INTO telegram_history(user_id,request_id,request_json) VALUES(?,?,?)', (user_id,request.request_id,data))
            c.execute('INSERT INTO telegram_active VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET request_json=excluded.request_json', (user_id,data))

    def get_offset(self):
        with self.database.connection() as c:
            row = c.execute("SELECT value FROM telegram_state WHERE key='offset'").fetchone()
        return int(row[0]) if row else 0

    def set_offset(self, offset):
        with self.database.connection() as c:
            c.execute("INSERT INTO telegram_state VALUES('offset',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(offset),))


    def save_dialog(self, user_id, question, answer, request_id=None, *, epoch=0):
        """Append-only free dialog in the existing state table, not request history."""
        import json
        prefix = f'dialog:{int(user_id)}:{int(epoch)}:'
        with self.database.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT value FROM telegram_state WHERE key=?',(prefix+'sequence',)).fetchone()
            number = int(row[0])+1 if row else 1
            value = json.dumps({'request_id':request_id,'question':question,'answer':answer},ensure_ascii=False)
            c.execute('INSERT INTO telegram_state(key,value) VALUES(?,?)',(prefix+f'{number:012d}',value))
            c.execute('INSERT INTO telegram_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                      (prefix+'sequence',str(number)))

    def load_dialog(self, user_id, request_id=None, *, epoch=0):
        """Last ten relevant exchanges (20 messages); archived requests are excluded."""
        import json
        prefix = f'dialog:{int(user_id)}:{int(epoch)}:'
        from dialog_history import select_turns, MAX_TURN_CHARACTERS
        # Stream a bounded page; rejected long turns must not hide earlier ones.
        with self.database.connection() as c:
            cursor = c.execute("SELECT value FROM telegram_state WHERE key LIKE ? AND key<>? "
                "AND json_valid(value) AND json_type(value)='object' "
                "AND (json_extract(value,'$.request_id') IS NULL OR json_extract(value,'$.request_id')=?) "
                "AND length(json_extract(value,'$.question'))+length(json_extract(value,'$.answer'))<=? "
                "ORDER BY key DESC", (prefix+'%',prefix+'sequence',request_id,MAX_TURN_CHARACTERS))
            def turns():
                while rows := cursor.fetchmany(16):
                    for row in rows:
                        entry = json.loads(row[0])
                        if not isinstance(entry.get('question'), str) or not isinstance(entry.get('answer'),str): continue
                        yield [{'role':'user','content':entry['question']},
                               {'role':'assistant','content':entry['answer']}]
            return select_turns(turns())

    def save_calculation(self, user_id, calculation, *, epoch=0):
        prefix = f'calculation:{int(user_id)}:{int(epoch)}:'
        with self.database.connection() as c:
            c.execute('INSERT INTO telegram_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                (prefix+calculation.calculation_id,calculation.model_dump_json()))
            c.execute('INSERT INTO telegram_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                (prefix+'current',calculation.calculation_id))

    def load_calculation(self, user_id, *, epoch=0):
        from calculation_context import CalculationState
        prefix = f'calculation:{int(user_id)}:{int(epoch)}:'
        with self.database.connection() as c:
            row = c.execute('SELECT value FROM telegram_state WHERE key=(SELECT ?||value FROM telegram_state WHERE key=?)',
                (prefix,prefix+'current')).fetchone()
        return CalculationState.model_validate_json(row[0]) if row else None
