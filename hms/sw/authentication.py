"""1.2.3 Authentication: verify credentials and manage sessions.

Secret: credential store config/users.json (PBKDF2-SHA256 hashes), random session tokens,
in-memory session table.
"""
import hashlib
import json
import os
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from hms.behaviour import timing_policy as tp
from hms.sw.event_store import EventType

_ITER = 100_000


class Role(Enum):
    NURSE = "NURSE"
    PHARMACIST = "PHARMACIST"
    TECHNICIAN = "TECHNICIAN"
    HOSPITAL_ADMIN = "HOSPITAL_ADMIN"
    IT_SPECIALIST = "IT_SPECIALIST"


@dataclass(frozen=True)
class Session:
    token: str
    user: str
    role: Role
    expiry: datetime


class AuthUnavailable(Exception):
    pass


class InvalidCredentials(Exception):
    pass


class RoleNotAuthorized(Exception):
    pass


class Authentication:
    def __init__(self, store, clock, users_path: str = "config/users.json"):
        self._store = store
        self._clock = clock
        self._path = users_path
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}

    def sign_in(self, username: str, password: str) -> Session:
        now = self._clock.now()
        try:
            accounts = self._load()
        except (OSError, ValueError):
            self._store.append(EventType.LOGIN_FAILURE, "HMS", now, username, {"reason": "service unavailable"})
            raise AuthUnavailable("authentication temporarily unavailable")
        acct = accounts.get(username)
        if not acct or not acct.get("enabled") or not self._matches(password, acct):
            self._store.append(EventType.LOGIN_FAILURE, "HMS", now, username, {"reason": "invalid credentials"})
            raise InvalidCredentials("authentication error")
        try:
            role = Role(acct.get("role"))
        except ValueError:
            self._store.append(EventType.LOGIN_FAILURE, "HMS", now, username, {"reason": "role not authorized"})
            raise RoleNotAuthorized("access denied")
        s = Session(secrets.token_urlsafe(32), username, role, now + tp.SESSION_TIMEOUT)
        with self._lock:
            self._sessions[s.token] = s
        self._store.append(EventType.LOGIN_SUCCESS, "HMS", now, username)
        return s

    def is_authenticated(self, token: str | None) -> bool:
        with self._lock:
            s = self._sessions.get(token) if token else None
            return s is not None and self._clock.now() < s.expiry

    def session_of(self, token: str | None) -> Session | None:
        with self._lock:
            s = self._sessions.get(token) if token else None
            now = self._clock.now()
            if s is None or now >= s.expiry:
                return None
            s = Session(s.token, s.user, s.role, now + tp.SESSION_TIMEOUT)
            self._sessions[token] = s
            return s

    def sign_out(self, token) -> None:
        with self._lock:
            s = self._sessions.pop(token, None)
        if s:
            self._store.append(EventType.LOGOUT, "HMS", self._clock.now(), s.user)

    def set_account(self, username, role: Role | None, enabled: bool, password: str | None = None) -> None:
        with self._lock:
            accounts = self._load()
            acct = accounts.get(username, {})
            acct["role"] = role.value if role else None
            acct["enabled"] = bool(enabled)
            if password is not None:
                acct.update(self._hash(password))
            accounts[username] = acct
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(accounts, f, indent=2)

    def _load(self):
        with open(self._path, encoding="utf-8") as f:
            return json.load(f)

    def _hash(self, password, salt=None):
        salt = salt or os.urandom(16).hex()
        h = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _ITER).hex()
        return {"salt": salt, "hash": h}

    def _matches(self, password, acct):
        if "salt" not in acct or "hash" not in acct:
            return False
        return secrets.compare_digest(self._hash(password or "", acct["salt"])["hash"], acct["hash"])
