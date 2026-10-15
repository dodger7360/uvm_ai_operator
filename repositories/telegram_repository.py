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
        # Filter in SQL before limiting, without loading the entire history.
        with self.database.connection() as c:
            rows = c.execute("SELECT value FROM telegram_state WHERE key LIKE ? AND key<>? "
                             "AND (json_extract(value,'$.request_id') IS NULL OR json_extract(value,'$.request_id')=?) "
                             "ORDER BY key DESC LIMIT 10", (prefix+'%',prefix+'sequence',request_id)).fetchall()
        messages = []
        for row in reversed(rows):
            entry = json.loads(row[0])
            if len(entry['question'])+len(entry['answer']) > 8000: continue
            messages.extend([{'role':'user','content':entry['question']},
                             {'role':'assistant','content':entry['answer']}])
        return messages
