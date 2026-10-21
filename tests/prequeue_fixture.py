"""Small frozen pre-queue schema/state; no historical code or Git required."""
from pathlib import Path

def seed_prequeue(database):
    source=Path(__file__).parent/'fixtures'/'prequeue_synthetic.sql'
    with database.connection() as connection:
        connection.executescript(source.read_text(encoding='utf-8'))
