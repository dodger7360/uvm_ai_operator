"""Optional, additive rule store. No request/history import and no automatic learning."""
from normalization_policy import GradeRule


class NormalizationPolicyRepository:
    def __init__(self, database, access):
        self.database, self.access = database, access
        with database.connection() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS normalization_rule (
                    rule_id TEXT NOT NULL, version INTEGER NOT NULL,
                    payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'candidate',
                    regression_evidence TEXT, approved_by INTEGER,
                    PRIMARY KEY(rule_id, version));
                CREATE TABLE IF NOT EXISTS normalization_rule_event (
                    id INTEGER PRIMARY KEY, rule_id TEXT NOT NULL, version INTEGER NOT NULL,
                    action TEXT NOT NULL, actor_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            ''')

    def propose(self, rule: GradeRule):
        with self.database.connection() as c:
            c.execute('INSERT INTO normalization_rule(rule_id,version,payload) VALUES(?,?,?)',
                      (rule.rule_id, rule.version, rule.model_dump_json()))

    def decide(self, rule_id, version, actor_id, *, approve, regression_evidence=None):
        # Use the existing server-side authorization, never a role from an AI response.
        if not self.access.is_admin(actor_id):
            raise PermissionError('Утверждать глобальные правила может только администратор.')
        if approve and not (regression_evidence and regression_evidence.strip()):
            raise ValueError('Нужна ссылка на успешную регрессионную проверку этой версии.')
        with self.database.connection() as c:
            row = c.execute('SELECT status FROM normalization_rule WHERE rule_id=? AND version=?',
                            (rule_id, version)).fetchone()
            if not row:
                raise ValueError('Версия правила не найдена.')
            if approve and row['status'] != 'candidate':
                raise ValueError('Для повторного утверждения создайте новую версию.')
            if approve:
                c.execute("UPDATE normalization_rule SET status='retired' WHERE rule_id=? AND status='approved'", (rule_id,))
            status = 'approved' if approve else 'rejected'
            c.execute('UPDATE normalization_rule SET status=?, regression_evidence=?, approved_by=? WHERE rule_id=? AND version=?',
                      (status, regression_evidence, actor_id, rule_id, version))
            c.execute('INSERT INTO normalization_rule_event(rule_id,version,action,actor_id) VALUES(?,?,?,?)',
                      (rule_id, version, status, actor_id))

    def active_rules(self):
        with self.database.connection() as c:
            rows = c.execute("SELECT payload FROM normalization_rule WHERE status='approved' ORDER BY rule_id,version").fetchall()
        return tuple(GradeRule.model_validate_json(row[0]) for row in rows)
