"""Offline Secure Vault foundation for a *trusted*, owner-authenticated runtime.

No HTTP endpoint, model tool, password parser, OS key installer or production
activation is exposed. Callers and key providers MUST be supplied by a trusted
host adapter, never reconstructed from model text. Plaintext lives only in the
caller's memory; SQLite and audit records store no secret values in the clear.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from threading import RLock
from typing import Callable, Iterator
from uuid import uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


_APP_ID = 0x41455654  # AEVT: never confuse with another project SQLite.
_VERSION = 1
_IDENTIFIER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}\Z")
_MAX_SECRET = 65_536
_MAX_BACKUP = 128 * 1024 * 1024
_BACKUP_MAGIC = b"AE-VAULT-BACKUP-1\0"
_OPS = frozenset({"save", "get", "list", "update"})


class VaultDenied(PermissionError):
    def __init__(self) -> None:
        super().__init__("Vault access denied")


class VaultUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Vault unavailable or damaged")


@dataclass(frozen=True, slots=True)
class VaultCaller:
    """Trusted-session identity, NOT a bearer token or an authentication API.

    A model or HTTP client must not be allowed to choose `is_owner` or any ID.
    """

    owner_id: str
    project_id: str
    actor_id: str
    is_owner: bool = False


@dataclass(frozen=True, slots=True)
class SecretMetadata:
    secret_id: str
    project_id: str
    category_id: str
    label: str
    kind: str
    revision: int
    archived: bool


def _identifier(value: object) -> str:
    if type(value) is not str or not _IDENTIFIER.fullmatch(value):
        raise VaultDenied()
    return value


def _label(value: object) -> str:
    if (type(value) is not str or not 1 <= len(value) <= 128
            or value.strip() != value
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise VaultDenied()
    return value


def _secret_bytes(value: object) -> bytes:
    if type(value) is not bytes or not 0 < len(value) <= _MAX_SECRET:
        raise VaultDenied()
    return value


def _key(key: object) -> bytes:
    if type(key) is not bytes or len(key) != 32:
        raise VaultUnavailable()
    return key


def _aad(owner_id: str, project_id: str, secret_id: str,
         revision: int, kind: str) -> bytes:
    return (f"vault-v1\0{owner_id}\0{project_id}\0{secret_id}"
            f"\0{revision}\0{kind}").encode("utf-8")


def _private_file(root: Path) -> Path:
    if os.name != "posix" or not isinstance(root, Path) or not root.is_absolute():
        raise VaultUnavailable()
    try:
        root.mkdir(mode=0o700, exist_ok=True)
        root_stat = os.lstat(root)
        if (not stat.S_ISDIR(root_stat.st_mode)
                or root_stat.st_uid != os.geteuid()
                or stat.S_IMODE(root_stat.st_mode) != 0o700):
            raise VaultUnavailable()
        directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            try:
                fd = os.open("vault.sqlite3", os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
            except FileExistsError:
                fd = os.open("vault.sqlite3", os.O_RDONLY | os.O_NOFOLLOW,
                             dir_fd=directory_fd)
            try:
                metadata = os.fstat(fd)
                if (not stat.S_ISREG(metadata.st_mode)
                        or metadata.st_nlink != 1
                        or metadata.st_uid != os.geteuid()
                        or stat.S_IMODE(metadata.st_mode) != 0o600):
                    raise VaultUnavailable()
            finally:
                os.close(fd)
        finally:
            os.close(directory_fd)
        return root / "vault.sqlite3"
    except (OSError, ValueError) as exc:
        raise VaultUnavailable() from None


class SecureVault:
    """Owner-scoped encrypted secrets, dynamic categories, grants and audit.

    `key_provider()` must return the SAME externally managed 32-byte key on
    each restart. This implementation never generates or saves a production
    master key, and therefore cannot independently install an auto-unseal.
    """

    def __init__(self, root: Path, *, owner_id: str,
                 key_provider: Callable[[], bytes]) -> None:
        self._owner_id = _identifier(owner_id)
        if not callable(key_provider):
            raise VaultUnavailable()
        self._key_provider = key_provider
        self._lock = RLock()
        path = _private_file(root)
        try:
            self._db = sqlite3.connect(str(path), timeout=5, check_same_thread=False)
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA journal_mode=DELETE")
            self._db.execute("PRAGMA secure_delete=ON")
            self._schema()
        except (sqlite3.Error, VaultUnavailable):
            if hasattr(self, "_db"):
                self._db.close()
            raise VaultUnavailable() from None

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _schema(self) -> None:
        app = self._db.execute("PRAGMA application_id").fetchone()[0]
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if (app, version) == (0, 0):
            if self._db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                raise VaultUnavailable()
            with self._transaction():
                self._db.executescript("""
                    CREATE TABLE vault_meta(owner_id TEXT NOT NULL PRIMARY KEY);
                    CREATE TABLE projects(project_id TEXT PRIMARY KEY);
                    CREATE TABLE categories(
                        category_id TEXT PRIMARY KEY, project_id TEXT NOT NULL
                            REFERENCES projects(project_id),
                        parent_id TEXT REFERENCES categories(category_id),
                        name TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0
                            CHECK(archived IN (0,1)));
                    CREATE TABLE secrets(
                        secret_id TEXT PRIMARY KEY, project_id TEXT NOT NULL
                            REFERENCES projects(project_id),
                        category_id TEXT NOT NULL REFERENCES categories(category_id),
                        label TEXT NOT NULL, kind TEXT NOT NULL,
                        revision INTEGER NOT NULL, nonce BLOB NOT NULL,
                        ciphertext BLOB NOT NULL, archived INTEGER NOT NULL DEFAULT 0
                            CHECK(archived IN (0,1)));
                    CREATE TABLE grants(
                        secret_id TEXT NOT NULL REFERENCES secrets(secret_id)
                            ON DELETE CASCADE,
                        actor_id TEXT NOT NULL, project_id TEXT NOT NULL
                            REFERENCES projects(project_id),
                        operation TEXT NOT NULL, expires_at INTEGER NOT NULL,
                        PRIMARY KEY(secret_id,actor_id,project_id,operation));
                    CREATE TABLE category_grants(
                        category_id TEXT NOT NULL REFERENCES categories(category_id),
                        actor_id TEXT NOT NULL, project_id TEXT NOT NULL
                            REFERENCES projects(project_id),
                        expires_at INTEGER NOT NULL,
                        PRIMARY KEY(category_id,actor_id,project_id));
                    CREATE TABLE audit(
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        occurred_at INTEGER NOT NULL, actor_id TEXT NOT NULL,
                        project_id TEXT NOT NULL, verb TEXT NOT NULL,
                        object_id TEXT NOT NULL, outcome TEXT NOT NULL);
                    CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit
                      BEGIN SELECT RAISE(ABORT,'audit immutable'); END;
                    CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit
                      BEGIN SELECT RAISE(ABORT,'audit immutable'); END;
                """)
                self._db.execute("INSERT INTO vault_meta VALUES (?)", (self._owner_id,))
                self._db.execute(f"PRAGMA application_id={_APP_ID}")
                self._db.execute(f"PRAGMA user_version={_VERSION}")
        elif (app, version) != (_APP_ID, _VERSION):
            raise VaultUnavailable()
        owner_row = self._db.execute("SELECT owner_id FROM vault_meta").fetchone()
        if owner_row != (self._owner_id,) or self._db.execute(
                "PRAGMA quick_check").fetchone() != ("ok",):
            raise VaultUnavailable()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._db.rollback()
            raise
        else:
            self._db.commit()

    def _caller(self, caller: VaultCaller) -> None:
        if (type(caller) is not VaultCaller or caller.owner_id != self._owner_id):
            raise VaultDenied()
        _identifier(caller.project_id)
        _identifier(caller.actor_id)
        if type(caller.is_owner) is not bool:
            raise VaultDenied()

    def _cipher(self) -> AESGCM:
        # A failing host key provider must never leak its payload/diagnostics
        # through exception text, structured model replies or API errors.
        try:
            return AESGCM(_key(self._key_provider()))
        except Exception:
            raise VaultUnavailable() from None

    def _require_owner(self, caller: VaultCaller) -> None:
        self._caller(caller)
        if not caller.is_owner:
            raise VaultDenied()

    def _audit(self, caller: VaultCaller, verb: str,
               object_id: str, outcome: str = "ok") -> None:
        self._db.execute(
            "INSERT INTO audit(occurred_at,actor_id,project_id,verb,object_id,outcome)"
            " VALUES(?,?,?,?,?,?)",
            (int(time.time()), caller.actor_id, caller.project_id,
             verb, object_id, outcome))

    def _allowed(self, caller: VaultCaller, project_id: str,
                 secret_id: str, operation: str) -> None:
        self._caller(caller)
        if caller.is_owner:
            return
        if operation not in _OPS or not self._db.execute(
            "SELECT 1 FROM grants WHERE secret_id=? AND actor_id=?"
            " AND project_id=? AND operation=? AND expires_at>?",
            (secret_id, caller.actor_id, caller.project_id,
             operation, int(time.time()))).fetchone():
            raise VaultDenied()

    def _category_active(self, category_id: str) -> tuple[str, bool]:
        """Resolve current namespace and archive state, including ancestors."""
        seen: set[str] = set()
        current = category_id
        project_id = ""
        while current is not None:
            if current in seen:
                raise VaultUnavailable()
            seen.add(current)
            row = self._db.execute("SELECT project_id,parent_id,archived"
                                   " FROM categories WHERE category_id=?",
                                   (current,)).fetchone()
            if row is None or row[2]:
                return project_id, False
            if not project_id:
                project_id = row[0]
            elif row[0] != project_id:
                raise VaultUnavailable()
            current = row[1]
        return project_id, True

    def ensure_project(self, caller: VaultCaller, project_id: str) -> str:
        """Idempotent only for a project already approved by trusted owner."""
        self._require_owner(caller)
        _identifier(project_id)
        with self._lock, self._transaction():
            self._db.execute("INSERT OR IGNORE INTO projects VALUES (?)", (project_id,))
            row = self._db.execute("SELECT category_id FROM categories"
                                   " WHERE project_id=? AND parent_id IS NULL",
                                   (project_id,)).fetchone()
            if row:
                return row[0]
            category_id = "cat_" + uuid4().hex
            self._db.execute("INSERT INTO categories(category_id,project_id,name)"
                             " VALUES (?,?,?)", (category_id, project_id, project_id))
            self._audit(caller, "project_create", category_id)
            return category_id

    def create_category(self, caller: VaultCaller, parent_id: str,
                        name: str) -> str:
        self._require_owner(caller)
        _label(name)
        with self._lock, self._transaction():
            parent_project, active = self._category_active(parent_id)
            if not active:
                raise VaultDenied()
            category_id = "cat_" + uuid4().hex
            self._db.execute("INSERT INTO categories(category_id,project_id,parent_id,name)"
                             " VALUES (?,?,?,?)", (category_id, parent_project, parent_id, name))
            self._audit(caller, "category_create", category_id)
            return category_id

    def rename_category(self, caller: VaultCaller, category_id: str,
                        name: str) -> None:
        self._require_owner(caller)
        _label(name)
        with self._lock, self._transaction():
            cursor = self._db.execute("UPDATE categories SET name=?"
                                      " WHERE category_id=? AND parent_id IS NOT NULL",
                                      (name, category_id))
            if cursor.rowcount != 1:
                raise VaultDenied()
            self._audit(caller, "category_rename", category_id)

    def move_category(self, caller: VaultCaller, category_id: str,
                      new_parent_id: str) -> None:
        self._require_owner(caller)
        with self._lock, self._transaction():
            old = self._db.execute("SELECT project_id,parent_id FROM categories"
                                   " WHERE category_id=?", (category_id,)).fetchone()
            parent = self._db.execute("SELECT project_id,archived FROM categories"
                                      " WHERE category_id=?", (new_parent_id,)).fetchone()
            if (old is None or old[1] is None or parent is None or parent[1]
                    or old[0] != parent[0]):
                raise VaultDenied()
            cursor = new_parent_id
            while cursor is not None:
                if cursor == category_id:
                    raise VaultDenied()
                row = self._db.execute("SELECT parent_id FROM categories"
                                       " WHERE category_id=?", (cursor,)).fetchone()
                cursor = row[0] if row else None
            self._db.execute("UPDATE categories SET parent_id=? WHERE category_id=?",
                             (new_parent_id, category_id))
            self._audit(caller, "category_move", category_id)

    def archive_category(self, caller: VaultCaller, category_id: str) -> None:
        self._require_owner(caller)
        with self._lock, self._transaction():
            if self._db.execute("UPDATE categories SET archived=1 WHERE"
                                " category_id=? AND parent_id IS NOT NULL"
                                " AND archived=0", (category_id,)).rowcount != 1:
                raise VaultDenied()
            self._audit(caller, "category_archive", category_id)

    def restore_category(self, caller: VaultCaller, category_id: str) -> None:
        self._require_owner(caller)
        with self._lock, self._transaction():
            if self._db.execute("UPDATE categories SET archived=0 WHERE"
                                " category_id=? AND parent_id IS NOT NULL"
                                " AND archived=1", (category_id,)).rowcount != 1:
                raise VaultDenied()
            self._audit(caller, "category_restore", category_id)

    def list_categories(self, caller: VaultCaller) -> tuple[tuple[str, str, str | None, str], ...]:
        self._require_owner(caller)
        with self._lock:
            return tuple(self._db.execute("SELECT category_id,project_id,parent_id,name"
                                          " FROM categories ORDER BY project_id,name").fetchall())

    def grant_category_save(self, caller: VaultCaller, category_id: str,
                            actor_id: str, expires_at: int) -> None:
        """Approved worker may then save newly generated keys without prompting."""
        self._require_owner(caller)
        _identifier(actor_id)
        if type(expires_at) is not int or expires_at <= time.time():
            raise VaultDenied()
        with self._lock, self._transaction():
            project_id, active = self._category_active(category_id)
            if not active:
                raise VaultDenied()
            self._db.execute("INSERT INTO category_grants VALUES (?,?,?,?)"
                             " ON CONFLICT(category_id,actor_id,project_id)"
                             " DO UPDATE SET expires_at=excluded.expires_at",
                             (category_id, actor_id, project_id, expires_at))
            self._audit(caller, "category_grant_save", category_id)

    def revoke_category_save(self, caller: VaultCaller, category_id: str,
                             actor_id: str) -> None:
        self._require_owner(caller)
        with self._lock, self._transaction():
            self._db.execute("DELETE FROM category_grants WHERE category_id=?"
                             " AND actor_id=?", (category_id, actor_id))
            self._audit(caller, "category_revoke_save", category_id)

    def save_secret(self, caller: VaultCaller, category_id: str, label: str,
                    kind: str, value: bytes) -> str:
        self._caller(caller)
        _label(label)
        _identifier(kind)
        _secret_bytes(value)
        with self._lock, self._transaction():
            project_id, active = self._category_active(category_id)
            if not active or project_id != caller.project_id:
                raise VaultDenied()
            if not caller.is_owner and not self._db.execute(
                "SELECT 1 FROM category_grants WHERE category_id=? AND"
                " actor_id=? AND project_id=? AND expires_at>?",
                (category_id, caller.actor_id, project_id,
                 int(time.time()))).fetchone():
                raise VaultDenied()
            secret_id = "sec_" + uuid4().hex
            revision = 1
            nonce = os.urandom(12)
            ciphertext = self._cipher().encrypt(
                nonce, value, _aad(self._owner_id, project_id, secret_id, revision, kind))
            self._db.execute("INSERT INTO secrets(secret_id,project_id,category_id,"
                             "label,kind,revision,nonce,ciphertext)"
                             " VALUES (?,?,?,?,?,?,?,?)",
                             (secret_id, project_id, category_id, label, kind,
                              revision, nonce, ciphertext))
            self._audit(caller, "save", secret_id)
            return secret_id

    def _record(self, secret_id: str) -> tuple:
        row = self._db.execute("SELECT project_id,category_id,label,kind,"
                               "revision,nonce,ciphertext,archived FROM secrets"
                               " WHERE secret_id=?", (secret_id,)).fetchone()
        if row is None:
            raise VaultDenied()
        return row

    def get_secret(self, caller: VaultCaller, secret_id: str) -> bytes:
        with self._lock:
            row = self._record(secret_id)
            self._allowed(caller, row[0], secret_id, "get")
            if row[7] or not self._category_active(row[1])[1]:
                raise VaultDenied()
            try:
                data = self._cipher().decrypt(
                    row[5], row[6],
                    _aad(self._owner_id, row[0], secret_id, row[4], row[3]))
            except (InvalidTag, ValueError, TypeError):
                raise VaultUnavailable() from None
            with self._transaction():
                self._audit(caller, "get", secret_id)
            return data

    def update_secret(self, caller: VaultCaller, secret_id: str,
                      value: bytes) -> int:
        _secret_bytes(value)
        with self._lock, self._transaction():
            row = self._record(secret_id)
            self._allowed(caller, row[0], secret_id, "update")
            if row[7] or not self._category_active(row[1])[1]:
                raise VaultDenied()
            version = row[4] + 1
            nonce = os.urandom(12)
            ciphertext = self._cipher().encrypt(
                nonce, value, _aad(self._owner_id, row[0], secret_id, version, row[3]))
            self._db.execute("UPDATE secrets SET revision=?,nonce=?,ciphertext=?"
                             " WHERE secret_id=?", (version, nonce, ciphertext, secret_id))
            self._audit(caller, "update", secret_id)
            return version

    def list_secrets(self, caller: VaultCaller) -> tuple[SecretMetadata, ...]:
        self._caller(caller)
        with self._lock:
            rows = self._db.execute("SELECT secret_id,project_id,category_id,"
                                    "label,kind,revision,archived FROM secrets"
                                    " ORDER BY label,secret_id").fetchall()
            output = []
            for item in rows:
                try:
                    self._allowed(caller, item[1], item[0], "list")
                except VaultDenied:
                    continue
                if not item[6] and self._category_active(item[2])[1]:
                    output.append(SecretMetadata(*item[:-1], bool(item[6])))
            with self._transaction():
                self._audit(caller, "list", "all")
            return tuple(output)

    def move_secret(self, caller: VaultCaller, secret_id: str,
                    target_category_id: str) -> None:
        """Move without changing the stable secret ID, revision or grants."""
        self._require_owner(caller)
        with self._lock, self._transaction():
            row = self._record(secret_id)
            target_project, active = self._category_active(target_category_id)
            if row[7] or not active or target_project != row[0]:
                raise VaultDenied()
            self._db.execute("UPDATE secrets SET category_id=? WHERE secret_id=?",
                             (target_category_id, secret_id))
            self._audit(caller, "secret_move", secret_id)

    def grant(self, caller: VaultCaller, secret_id: str, actor_id: str,
              target_project_id: str, operations: frozenset[str],
              expires_at: int) -> None:
        self._require_owner(caller)
        _identifier(actor_id)
        _identifier(target_project_id)
        if (type(operations) is not frozenset or not operations
                or not operations <= _OPS or type(expires_at) is not int
                or expires_at <= time.time()):
            raise VaultDenied()
        with self._lock, self._transaction():
            record = self._record(secret_id)
            if record[7] or not self._db.execute(
                "SELECT 1 FROM projects WHERE project_id=?",
                (target_project_id,)).fetchone():
                raise VaultDenied()
            for operation in operations:
                self._db.execute("INSERT INTO grants VALUES (?,?,?,?,?)"
                                 " ON CONFLICT(secret_id,actor_id,project_id,operation)"
                                 " DO UPDATE SET expires_at=excluded.expires_at",
                                 (secret_id, actor_id, target_project_id,
                                  operation, expires_at))
            self._audit(caller, "grant", secret_id)

    def revoke(self, caller: VaultCaller, secret_id: str,
               actor_id: str, target_project_id: str) -> None:
        self._require_owner(caller)
        with self._lock, self._transaction():
            self._db.execute("DELETE FROM grants WHERE secret_id=? AND actor_id=?"
                             " AND project_id=?", (secret_id, actor_id, target_project_id))
            self._audit(caller, "revoke", secret_id)

    def archive_secret(self, caller: VaultCaller, secret_id: str) -> None:
        self._require_owner(caller)
        with self._lock, self._transaction():
            if self._db.execute("UPDATE secrets SET archived=1 WHERE secret_id=?"
                                " AND archived=0", (secret_id,)).rowcount != 1:
                raise VaultDenied()
            self._audit(caller, "archive", secret_id)

    def restore_secret(self, caller: VaultCaller, secret_id: str) -> None:
        self._require_owner(caller)
        with self._lock, self._transaction():
            if self._db.execute("UPDATE secrets SET archived=0 WHERE secret_id=?"
                                " AND archived=1", (secret_id,)).rowcount != 1:
                raise VaultDenied()
            self._audit(caller, "restore", secret_id)

    def audit_events(self, caller: VaultCaller) -> tuple[tuple, ...]:
        self._require_owner(caller)
        with self._lock:
            return tuple(self._db.execute(
                "SELECT occurred_at,actor_id,project_id,verb,object_id,outcome"
                " FROM audit ORDER BY seq").fetchall())

    def encrypted_backup(self, caller: VaultCaller, backup_key: bytes) -> bytes:
        """Create an encrypted *in-memory* snapshot for off-host export.

        Owner's external backup transport must persist it off-host; this method
        does not pretend that an export in RAM is an off-host verified backup.
        """
        self._require_owner(caller)
        with self._lock:
            snapshot = self._db.serialize()
        if len(snapshot) > _MAX_BACKUP:
            raise VaultUnavailable()
        nonce = os.urandom(12)
        return (_BACKUP_MAGIC + nonce + AESGCM(_key(backup_key)).encrypt(
            nonce, snapshot, _BACKUP_MAGIC))

    @classmethod
    def restore_backup(cls, encrypted: bytes, backup_key: bytes,
                       destination: Path, *, owner_id: str,
                       key_provider: Callable[[], bytes]) -> SecureVault:
        """Offline-only restore into a **new** private directory, never live."""
        if (type(encrypted) is not bytes
                or not encrypted.startswith(_BACKUP_MAGIC)
                or len(encrypted) < len(_BACKUP_MAGIC) + 12 + 16
                or len(encrypted) > _MAX_BACKUP + len(_BACKUP_MAGIC) + 28
                or destination.exists()):
            raise VaultUnavailable()
        offset = len(_BACKUP_MAGIC)
        try:
            serialized = AESGCM(_key(backup_key)).decrypt(
                encrypted[offset:offset + 12], encrypted[offset + 12:], _BACKUP_MAGIC)
            staged = sqlite3.connect(":memory:")
            staged.deserialize(serialized)
            if (staged.execute("PRAGMA application_id").fetchone()[0] != _APP_ID
                    or staged.execute("PRAGMA user_version").fetchone()[0] != _VERSION
                    or staged.execute("SELECT owner_id FROM vault_meta").fetchone()
                    != (_identifier(owner_id),)
                    or staged.execute("PRAGMA integrity_check").fetchone() != ("ok",)):
                raise VaultUnavailable()
            restored = cls(destination, owner_id=owner_id,
                           key_provider=key_provider)
            with restored._lock:
                staged.backup(restored._db)
                restored._schema()
            staged.close()
            return restored
        except (sqlite3.Error, InvalidTag, ValueError, TypeError, VaultDenied):
            raise VaultUnavailable() from None


class VaultOrchestratorBridge:
    """In-process trusted owner-chat adapter; not wired to a chat provider."""

    def __init__(self, vault: SecureVault) -> None:
        self._vault = vault

    def save_from_owner_message(self, owner: VaultCaller, category_id: str,
                                label: str, kind: str, value: bytes) -> str:
        """Caller authenticates owner before invoking; no second approval."""
        self._vault._require_owner(owner)
        return self._vault.save_secret(owner, category_id, label, kind, value)

    def use_for_approved_task(self, actor: VaultCaller, secret_id: str,
                              consume: Callable[[bytes], bool]) -> bool:
        """Pass value to trusted task function, return only a safe success bit.

        The task function must not log, echo or persist plaintext. NEVER bind
        this method itself as an unrestricted LLM-invokable tool.
        """
        if not callable(consume):
            raise VaultDenied()
        result = consume(self._vault.get_secret(actor, secret_id))
        if type(result) is not bool:
            raise VaultDenied()  # Do not accidentally echo value in task result.
        return result
