"""
login_limiter.py — brute-force protection for the access-code login gate.

Going internet-facing (Coolify/HTTPS) changes the threat model: the login page is
reachable by anyone, so a bare "0.8s delay on wrong password" is no longer enough.
This module adds two layers, both in-memory (single gunicorn worker — see note):

  * PER-IP LOCKOUT — after `max_failures` wrong attempts from one IP within
    `window_seconds`, that IP is locked out for `lockout_seconds`.
  * GLOBAL BACKSTOP — after `global_max_failures` wrong attempts from ANY mix of
    IPs within the window, ALL logins are locked for `global_lockout_seconds`.
    This blunts distributed guessing that rotates IPs. Alex can still get in
    after the window; with a strong ACCESS_CODE the search space is hopeless
    for an attacker anyway — this exists to make the noise loud and slow.

A successful login clears that IP's failure history (so Alex fat-fingering the
code twice doesn't stack up forever). Lockout state is checked BEFORE the
password is compared, so attempts during a lockout reveal nothing.

NOTE: state is per-process. The server runs a single gunicorn worker today; if
that ever changes, move this state to the shared SQLite/Supabase layer.
"""

import threading
import time


class LoginLimiter:
    def __init__(self, max_failures=5, lockout_seconds=900, window_seconds=900,
                 global_max_failures=20, global_lockout_seconds=900, now=time.time):
        self.max_failures = max_failures
        self.lockout_seconds = lockout_seconds
        self.window_seconds = window_seconds
        self.global_max_failures = global_max_failures
        self.global_lockout_seconds = global_lockout_seconds
        self._now = now
        self._lock = threading.Lock()
        self._failures = {}        # ip -> [timestamps within window]
        self._locked_until = {}    # ip -> unix ts
        self._global_failures = []  # timestamps within window, all IPs
        self._global_locked_until = 0.0

    # -- internals -------------------------------------------------------------
    def _prune(self, now):
        cutoff = now - self.window_seconds
        for ip in list(self._failures):
            kept = [t for t in self._failures[ip] if t > cutoff]
            if kept:
                self._failures[ip] = kept
            else:
                del self._failures[ip]
        self._global_failures = [t for t in self._global_failures if t > cutoff]
        for ip in list(self._locked_until):
            if self._locked_until[ip] <= now:
                del self._locked_until[ip]

    # -- public API ------------------------------------------------------------
    def allowed(self, ip):
        """Return (allowed: bool, retry_after_seconds: int)."""
        with self._lock:
            now = self._now()
            self._prune(now)
            if self._global_locked_until > now:
                return False, int(self._global_locked_until - now) + 1
            until = self._locked_until.get(ip, 0)
            if until > now:
                return False, int(until - now) + 1
            return True, 0

    def record_failure(self, ip):
        """Record a wrong attempt. Returns (failure_count, locked_now: bool) —
        locked_now is True only on the attempt that TRIPS a lockout, so the
        caller can report the incident exactly once."""
        with self._lock:
            now = self._now()
            self._prune(now)
            self._failures.setdefault(ip, []).append(now)
            self._global_failures.append(now)
            count = len(self._failures[ip])
            tripped = False
            if count >= self.max_failures and self._locked_until.get(ip, 0) <= now:
                self._locked_until[ip] = now + self.lockout_seconds
                tripped = True
            if (len(self._global_failures) >= self.global_max_failures
                    and self._global_locked_until <= now):
                self._global_locked_until = now + self.global_lockout_seconds
                tripped = True
            return count, tripped

    def record_success(self, ip):
        """A correct login clears that IP's failure history and lockout."""
        with self._lock:
            self._failures.pop(ip, None)
            self._locked_until.pop(ip, None)
