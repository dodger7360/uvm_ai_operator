"""Bounded restart supervisor. Never imports the bot or reads .env/SQLite."""
import argparse
from collections import deque
import logging
from pathlib import Path
import subprocess
import sys
import time

from windows_runtime import AlreadyRunning, ProcessLock, maintenance_requested, wait_or_maintenance

LOG = logging.getLogger('telegram_supervisor')
PERMANENT_CODES = {2, 3, 4, 5, 7}


class RestartPolicy:
    def __init__(self, initial_attempts=None, base_delay=30, max_delay=300,
                 window=900, max_failures=5, cooldown=900):
        self.initial_attempts = initial_attempts
        self.initial_failures = 0
        self.ever_ready = False
        self.failures = deque()
        self.base_delay, self.max_delay = base_delay, max_delay
        self.window, self.max_failures, self.cooldown = window, max_failures, cooldown

    def next_delay(self, exit_code, ready, now):
        self.ever_ready |= ready
        if exit_code == 0 or exit_code in PERMANENT_CODES:
            return None
        if not self.ever_ready:
            self.initial_failures += 1
            if self.initial_attempts and self.initial_failures >= self.initial_attempts:
                return None
        while self.failures and now - self.failures[0] >= self.window:
            self.failures.popleft()
        self.failures.append(now)
        if len(self.failures) >= self.max_failures:
            return self.cooldown
        return min(self.base_delay * 2 ** (len(self.failures) - 1), self.max_delay)


def supervise(root, command, policy=None, poll_interval=0.5):
    root = Path(root)
    policy = policy or RestartPolicy()
    ready_file = root / 'telegram.ready'
    try:
        with ProcessLock(root / 'telegram-supervisor.lock'):
            LOG.info('supervisor_started')
            while not maintenance_requested(root):
                # A crashed supervisor may leave its child alive. Wait for the
                # existing OS-held worker lock instead of creating another child.
                try:
                    with ProcessLock(root / 'telegram.lock'):
                        pass
                except AlreadyRunning:
                    LOG.info('existing_worker_lock_held wait_seconds=30')
                    if wait_or_maintenance(root, 30):
                        return 0
                    continue
                ready_file.unlink(missing_ok=True)
                try:
                    child = subprocess.Popen(command, cwd=root, stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
                except OSError as error:
                    LOG.error('child_launch_failed exception=%s', type(error).__name__)
                    code, ready = 1, False
                else:
                    LOG.info('child_launched pid=%s', child.pid)
                    ready = False
                    while child.poll() is None:
                        ready |= ready_file.exists()
                        # Maintenance is cooperative: never kill a request in flight.
                        time.sleep(poll_interval)
                    code = child.returncode
                    ready |= ready_file.exists()
                    LOG.log(logging.INFO if code == 0 else logging.ERROR,
                            'child_exited pid=%s exit_code=%s ready=%s', child.pid, code, ready)
                if maintenance_requested(root):
                    LOG.info('maintenance_stop no_restart=true')
                    return 0
                delay = 30 if code == 3 else policy.next_delay(code, ready, time.monotonic())
                if delay is None:
                    LOG.info('restart_stopped exit_code=%s operator_action_required=%s', code, code != 0)
                    return 0  # Scheduler must not retry a permanent error/maintenance exit.
                LOG.warning('restart_scheduled delay_seconds=%s', delay)
                if wait_or_maintenance(root, delay):
                    LOG.info('maintenance_stop no_restart=true')
                    return 0
            LOG.info('maintenance_stop no_restart=true')
            return 0
    except AlreadyRunning:
        LOG.info('supervisor_already_running no_second_child=true')
        return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--initial-attempts', type=int)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    logging.basicConfig(filename=root / 'telegram-supervisor.log', level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s', encoding='utf-8')
    command = [str(root / '.venv' / 'Scripts' / 'pythonw.exe'), str(root / 'main.py')]
    try:
        return supervise(root, command, RestartPolicy(initial_attempts=args.initial_attempts))
    except Exception as error:
        LOG.error('supervisor_failed exception=%s', type(error).__name__)
        return 1  # Scheduler retries supervisor failures, with its own finite budget.


if __name__ == '__main__':
    raise SystemExit(main())
