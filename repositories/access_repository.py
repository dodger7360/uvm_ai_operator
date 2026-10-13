"""Persistent access control on the existing Database layer."""
import hashlib
import hmac
import time


class StaleAccessAction(ValueError):
    pass


class AccessNotificationFailed(RuntimeError):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS telegram_access_user (
    user_id INTEGER PRIMARY KEY,
    role TEXT NOT NULL CHECK(role IN ('admin','employee')),
    status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected','revoked')),
    display_name TEXT NOT NULL,
    username TEXT,
    requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    approved_at TEXT,
    changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    acted_by INTEGER,
    revision INTEGER NOT NULL DEFAULT 1,
    context_floor INTEGER NOT NULL DEFAULT 0,
    admin_notified_at TEXT,
    notify_attempt_at REAL NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS telegram_single_admin
    ON telegram_access_user(role) WHERE role='admin';
CREATE TABLE IF NOT EXISTS telegram_access_event (
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, status TEXT NOT NULL,
    acted_by INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS telegram_access_bootstrap (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    token_hash TEXT NOT NULL, expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS telegram_access_claim_attempt (
    user_id INTEGER PRIMARY KEY, count INTEGER NOT NULL, started_at REAL NOT NULL
);
"""


def user_details(user):
    name = ' '.join(str(user.get(key, '')).strip() for key in ('first_name','last_name')).strip()
    return name[:128] or 'Сотрудник', (user.get('username') or '')[:64] or None


class AccessRepository:
    def __init__(self, database):
        self.database = database
        with database.connection() as connection:
            connection.executescript(SCHEMA)

    def get(self, user_id):
        with self.database.connection() as connection:
            row = connection.execute('SELECT * FROM telegram_access_user WHERE user_id=?', (user_id,)).fetchone()
        return dict(row) if row else None

    def admin_id(self):
        with self.database.connection() as connection:
            row = connection.execute("SELECT user_id FROM telegram_access_user WHERE role='admin' AND status='approved'").fetchone()
        return row[0] if row else None

    def allowed(self, user_id):
        row = self.get(user_id)
        # A valid initial admin is always approved; role alone cannot bypass status.
        return bool(row and row['status'] == 'approved')

    def is_admin(self, user_id):
        row = self.get(user_id)
        return bool(row and row['role'] == 'admin' and row['status'] == 'approved')

    @staticmethod
    def _assert_admin(connection, user_id):
        row = connection.execute("SELECT 1 FROM telegram_access_user WHERE user_id=? AND role='admin' AND status='approved'", (user_id,)).fetchone()
        if row is None:
            raise PermissionError('Administrative access required.')

    @staticmethod
    def _add_admin(connection, user_id, user):
        existing = connection.execute("SELECT user_id FROM telegram_access_user WHERE role='admin'").fetchone()
        if existing:
            if existing[0] != user_id:
                raise PermissionError('Administrator already bound.')
            return False
        if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
            raise ValueError('Invalid configured administrator.')
        name, username = user_details(user)
        connection.execute("""INSERT INTO telegram_access_user(user_id,role,status,display_name,username,approved_at,acted_by)
            VALUES(?,'admin','approved',?,?,CURRENT_TIMESTAMP,?)
            ON CONFLICT(user_id) DO UPDATE SET role='admin',status='approved',display_name=excluded.display_name,
            username=excluded.username,approved_at=CURRENT_TIMESTAMP,changed_at=CURRENT_TIMESTAMP,
            acted_by=excluded.acted_by,revision=telegram_access_user.revision+1,context_floor=0""", (user_id,name,username,user_id))
        connection.execute("INSERT INTO telegram_access_event(user_id,status,acted_by) VALUES(?,'approved',?)", (user_id,user_id))
        connection.execute('DELETE FROM telegram_access_bootstrap')
        return True

    def bootstrap_configured_admin(self, user_id):
        """Only a trusted deployment configuration may call this method."""
        with self.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            return self._add_admin(connection, user_id, {'first_name':'Администратор'})

    def issue_binding(self, token, ttl=1800):
        """Operator-issued random secret; never bind the first /start visitor."""
        if len(token) < 24 or ttl <= 0:
            raise ValueError('Binding token must be high entropy and time limited.')
        with self.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            if connection.execute("SELECT 1 FROM telegram_access_user WHERE role='admin'").fetchone():
                raise PermissionError('Administrator already bound.')
            connection.execute('INSERT INTO telegram_access_bootstrap VALUES(1,?,?) ON CONFLICT(singleton) DO UPDATE SET token_hash=excluded.token_hash,expires_at=excluded.expires_at',
                               (hashlib.sha256(token.encode()).hexdigest(),time.time()+ttl))

    def claim_admin(self, user, token):
        user_id, now = user['id'], time.time()
        with self.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            if connection.execute("SELECT 1 FROM telegram_access_user WHERE role='admin'").fetchone():
                return False
            attempt = connection.execute('SELECT count,started_at FROM telegram_access_claim_attempt WHERE user_id=?', (user_id,)).fetchone()
            count = attempt[0] if attempt and now-attempt[1] < 1800 else 0
            started = attempt[1] if count else now
            if count >= 5:
                return False
            connection.execute('INSERT INTO telegram_access_claim_attempt VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET count=excluded.count,started_at=excluded.started_at', (user_id,count+1,started))
            binding = connection.execute('SELECT token_hash,expires_at FROM telegram_access_bootstrap WHERE singleton=1').fetchone()
            valid = binding and binding[1] > now and hmac.compare_digest(binding[0], hashlib.sha256(token.encode()).hexdigest())
            if not valid:
                return False
            return self._add_admin(connection, user_id, user)

    def request_access(self, user):
        name, username = user_details(user)
        with self.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            cursor = connection.execute("INSERT OR IGNORE INTO telegram_access_user(user_id,role,status,display_name,username) VALUES(?,'employee','pending',?,?)", (user['id'],name,username))
            created = cursor.rowcount == 1
            connection.execute('UPDATE telegram_access_user SET display_name=?,username=? WHERE user_id=?', (name,username,user['id']))
            if created:
                connection.execute("INSERT INTO telegram_access_event(user_id,status) VALUES(?,'pending')", (user['id'],))
        return created

    def list_users(self, status=None, limit=100):
        with self.database.connection() as connection:
            query = "SELECT * FROM telegram_access_user WHERE role='employee'"
            params = []
            if status:
                query += ' AND status=?'
                params.append(status)
            rows = connection.execute(query+' ORDER BY requested_at,user_id LIMIT ?', (*params,limit)).fetchall()
        return [dict(row) for row in rows]

    def reserve_notification(self, user_id):
        now = time.time()
        with self.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            cursor = connection.execute("UPDATE telegram_access_user SET notify_attempt_at=? WHERE user_id=? AND status='pending' AND admin_notified_at IS NULL AND notify_attempt_at<?", (now,user_id,now-60))
            return cursor.rowcount == 1

    def notification_sent(self, user_id):
        with self.database.connection() as connection:
            connection.execute("UPDATE telegram_access_user SET admin_notified_at=CURRENT_TIMESTAMP WHERE user_id=? AND status='pending'", (user_id,))

    def transition(self, actor, user_id, action, revision, notify=None):
        """Atomic CAS, server-side role check, and fail-closed approval notification.

        Approval commits only after its notification succeeds. Denial/revocation
        commits regardless of an unreachable target, so access stays blocked.
        """
        states = {'approve':'approved','reject':'rejected','revoke':'revoked'}
        if action not in states:
            raise StaleAccessAction('Unknown action.')
        with self.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            self._assert_admin(connection, actor)
            row = connection.execute('SELECT * FROM telegram_access_user WHERE user_id=?', (user_id,)).fetchone()
            if not row or row['role'] == 'admin' or row['revision'] != revision:
                raise StaleAccessAction('Stale action or protected administrator.')
            if (action == 'approve' and row['status'] not in {'pending','rejected','revoked'}) or (action == 'reject' and row['status'] != 'pending') or (action == 'revoke' and row['status'] != 'approved'):
                raise StaleAccessAction('This action no longer applies.')
            floor = row['context_floor']
            if action == 'approve':
                floor = connection.execute('SELECT COALESCE(MAX(id),0) FROM telegram_history WHERE user_id=?', (user_id,)).fetchone()[0]
            connection.execute('''UPDATE telegram_access_user SET status=?,changed_at=CURRENT_TIMESTAMP,acted_by=?,
                revision=revision+1,context_floor=?,approved_at=CASE WHEN ?='approved' THEN CURRENT_TIMESTAMP ELSE approved_at END
                WHERE user_id=?''', (states[action],actor,floor,states[action],user_id))
            connection.execute('INSERT INTO telegram_access_event(user_id,status,acted_by) VALUES(?,?,?)', (user_id,states[action],actor))
            if action == 'approve' and (notify is None or not notify(user_id)):
                raise AccessNotificationFailed('Approval notification failed; access remains blocked.')
        return states[action]


class AuthorizedRequestRepository:
    """Reuse request storage; hide archived context without deleting any data."""
    def __init__(self, requests, access):
        self.requests, self.access = requests, access

    def load(self, user_id):
        access = self.access.get(user_id)
        if not access or access['status'] != 'approved':
            raise PermissionError('Access is blocked.')
        if access['role'] != 'admin':
            with self.requests.database.connection() as connection:
                row = connection.execute('SELECT COALESCE(MAX(id),0) FROM telegram_history WHERE user_id=?', (user_id,)).fetchone()
            if row[0] <= access['context_floor']:
                return None
        return self.requests.load(user_id)

    def save(self, user_id, request):
        if not self.access.allowed(user_id):
            raise PermissionError('Access is blocked.')
        self.requests.save(user_id, request)
