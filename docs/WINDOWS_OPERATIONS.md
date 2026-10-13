# Windows deployment and recovery

Local operational changes on top of `8e39083`; not automatically committed or pushed.
No changes to normalization, AI model/effort, SQLite schema, credentials or dependency versions.

## Start and stop

Run `start-telegram.ps1` once. It launches the existing virtual environment's
pythonw with `windows_supervisor.py`, hidden and independent of the terminal.
The explicit start clears `telegram.maintenance`. The first manual startup has
a two-attempt budget (30 seconds between attempts). A permanent failure stops
without retries. After readiness, crash recovery uses the normal rolling budget.

For planned maintenance run `stop-telegram.ps1`. It creates the persistent
`telegram.maintenance` marker. The bot finishes a request already in progress,
saves its offset, and exits before the next update. An outstanding long poll
may take up to the HTTP timeout; a long AI operation may take longer. The stop
script waits 180 seconds and never kills a request. If it times out, inspect
safely and wait; do not clear the marker or launch another worker.

Keep the marker throughout code/database maintenance. Neither the scheduler nor
the supervisor clears it. Explicit `start-telegram.ps1` resumes operation.
Unexpected termination without this marker is considered eligible for recovery.
Do not stop the scheduled task alone for maintenance: use the marker first.

## Scheduler

`install-telegram-task.ps1` registers `UVM_SteelBot` at the current user's logon,
with a 30-second delay. It uses Interactive logon and Limited privileges;
no password is stored, no administrative account or system startup is required.
The task directly executes `.venv/Scripts/pythonw.exe windows_supervisor.py`,
with the project working directory. No interactive PowerShell remains running.

Scheduler settings: IgnoreNew for overlapping triggers, unlimited task runtime,
three retries of an abnormal supervisor failure at one-minute intervals,
allowed on battery, and start when available. Normal exit for maintenance or a
permanent application error does not trigger scheduler retries.
Logoff ends Interactive execution; the next logon resumes it unless maintenance
is active. System-start execution before user logon is not configured.

If task registration is denied by corporate policy, do not elevate or bypass it.
Ask IT to approve/register this exact least-privileged task. An approved Startup
folder shortcut to pythonw + supervisor is an alternative proposal, not installed
automatically when a policy rejects the task. Interactive UI remains unnecessary.

## Recovery and singleton protection

`ProcessLock` holds a Windows `msvcrt` byte-range lock for the process lifetime.
The worker retains the existing `telegram.lock` path and byte so independently
started old/new workers cannot poll together. A separate supervisor lock prevents
multiple watchdogs. PID/ready files are diagnostic only, never the lock authority.
The OS releases locks on process death. The supervisor waits when an existing
worker owns the lock, including an orphan left after a supervisor crash.

Every child exit records timestamp, PID and exit code in
`telegram-supervisor.log`; no credentials, arguments or raw traceback are logged.
Restart delays are 30, 60, 120, 240, then up to 300 seconds. Five failures within
15 minutes introduce a 15-minute cooldown. Further transient failures continue
only at this bounded rate. The scheduler's startup uses this rolling policy;
the one-off manual deployment instead stops after two failed initial attempts.
Successful long-lived runs age previous failures out of the window.

Missing token, another worker, webhook conflict, authentication/proxy
configuration/409 errors, invalid admin configuration and other startup
configuration errors do not get rapidly retried. A duplicate-worker race waits
30 seconds for the existing lock, without any second polling.

## Network diagnostics and polling

Telegram still uses urllib and existing system/HTTP(S) proxy handling. A separate
OpenAI SOCKS proxy is not implicitly used by Telegram. TLS verification remains
on. Failures log only whitelisted method, exception class, safe category, phase,
attempt number and delay. Original exception strings, headers and token-bearing
URLs are never written. The phase is precise for DNS/TLS/HTTP response errors;
`request` means the exact TCP/TLS subphase is not known.

Temporary polling failures retry in the same worker after 5, 10, 20, 40, then
60 seconds, with recovery recorded as `polling_connected recovery=True`.
Startup transport failures exit for the supervisor to retry. Permanent 401/403/
407/409 stop polling and require operator review. Maintenance interrupts waits
but never interrupts an active normalization. A request can arrive in the short
interval between an operator's log check and maintenance; this residual risk
must be stated when replacing an older worker without cooperative support.

## Validation and local state

Use `python -X utf8 -m pytest` in the existing .venv on Windows. Tests use isolated
locks, mock transport, temporary SQLite and fake child processes. Never crash the
production worker or send Telegram messages to validate recovery.

Ignored local state: telegram.log, telegram.lock, telegram.ready,
telegram.maintenance, telegram-supervisor.log, telegram-supervisor.lock.
The ready marker is created only after service initialization and removed on
normal exit; it is not a substitute for a process/OS-lock check.
Back up live SQLite with sqlite3.Connection.backup; never copy a live WAL DB
as a bare file. All users, source rows and history must remain intact.
