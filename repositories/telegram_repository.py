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
