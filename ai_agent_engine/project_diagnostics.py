"""Stage 7.7/V: offline, owner-scoped diagnostic ledger and conservative QA.

Trusted Python callers only. No HTTP endpoint, LLM, subprocess execution of
diagnostic text, grant issuance, live repair, Netcup or Windows integration.
The project approval gate is reused, but diagnostic SQLite is independent
from both the project-memory and central task databases.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
from types import MappingProxyType
from typing import Callable, Iterator
from uuid import uuid4

from .project_leader import (
    ProjectLeaderStore, ProjectLeaderUnavailable, _check_directory,
    _database, _file,
)
from .task_api import OwnerScope, ProjectForbidden, _TASK_ID
from .task_versions import _COMMIT, _git, _snapshot, SourceVersionRejected


APPLICATION_ID = 0x41454447  # AEDG, not the task or project-memory SQLite.
SCHEMA_VERSION = 1
MAX_EXCERPT = 512  # offline fail-closed technical ceiling; not a retention policy.
MAX_OCCURRENCES = 1000
MAX_ATTEMPTS = 3
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}\Z")
_DANGEROUS = re.compile(
    r"(?i)(?:ignore\s+(?:all\s+)?(?:previous|system)\s+(?:rules|instructions)|"
    r"(?:^|\n)\s*(?:system|developer)\s*:|"
    r"\b(?:api[_-]?key|password|token|secret|authorization|private[_-]?key)"
    r"\s*[:=]\s*\S+|sk-[a-z0-9]{10,}|-----BEGIN\s+[^\n]+PRIVATE KEY-----|"
    r"(?:^|/)\.env(?:$|[/.])|(?:^|/)id_rsa(?:$|[/.])|"
    r"owner\s+approved\s+(?:a\s+)?(?:grant|deployment)|"
    r"(?:issue|grant)\s+(?:me\s+)?(?:a\s+)?(?:actiongrant|permission))"
)
_SIGNALS = frozenset({"task_failure", "test_failure", "build_failure",
                      "tool_failure", "confirmed_defect"})


class InvalidDiagnostic(ValueError):
    """Sanitized rejection of untrusted metadata."""


class DiagnosticHold(RuntimeError):
    """Unverifiable/unsafe action, without disclosing private evidence."""


@dataclass(frozen=True, slots=True)
class GitSource:
    """A fresh read-only observation of an actual local Git checkout.

    Never accept this object from model text or a network request.
    Every state-changing ledger operation re-observes the repository.
    """

    workspace: str
    baseline_commit: str
    result_commit: str
    patch_sha256: str | None


@dataclass(frozen=True, slots=True)
class FailureSignal:
    task_id: str
    kind: str
    error_code: str
    check_name: str
    expected: str
    actual: str
    excerpt: str


@dataclass(frozen=True, slots=True)
class Incident:
    incident_id: str
    task_id: str
    fingerprint: str
    baseline_commit: str
    status: str
    occurrences: int
    attempts: int


@dataclass(frozen=True, slots=True)
class Attempt:
    attempt_id: str
    incident_id: str
    slot: int
    hypothesis: str
    test_plan: str
    status: str
    result_commit: str | None


@dataclass(frozen=True, slots=True)
class Occurrence:
    occurrence_id: str
    incident_id: str
    delivery_key: str
    result_commit: str
    patch_sha256: str | None
    kind: str
    error_code: str
    check_name: str
    expected: str
    actual: str
    excerpt: str
    created_at: str


@dataclass(frozen=True, slots=True)
class QACheck:
    attempt_id: str
    name: str
    passed: bool
    source_commit: str
    source_patch: str | None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":")).encode("utf-8")).hexdigest()


def _key(value: object) -> str:
    if type(value) is not str or not _KEY.fullmatch(value):
        raise InvalidDiagnostic("invalid diagnostic identifier")
    return value


def _safe_text(value: object, limit: int) -> str:
    if (type(value) is not str or not value or value != value.strip()
            or len(value.encode("utf-8")) > limit
            or any((ord(c) < 32 and c not in "\n\t") or ord(c) == 127
                   for c in value)
            or _DANGEROUS.search(value)):
        raise DiagnosticHold("unsafe_or_oversized_diagnostic")
    return value


def capture_git_source(workspace: str | Path, baseline_commit: str) -> GitSource:
    """Observe Git HEAD + dirty bytes twice; baseline must be an ancestor.

    Reuses the 6.13 read-only Git inspection. A worktree with .git *file*,
    symlinks, unsupported index entries or uncertain source is held, not guessed.
    """
    if type(baseline_commit) is not str or not _COMMIT.fullmatch(baseline_commit):
        raise InvalidDiagnostic("invalid baseline")
    raw = os.path.abspath(os.fspath(workspace))
    if raw != os.path.realpath(raw):
        raise DiagnosticHold("unsafe_workspace")
    try:
        fd = os.open(raw, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if info.st_uid != os.geteuid() or info.st_mode & 0o022:
                raise DiagnosticHold("unsafe_workspace")
            first = _snapshot(raw, fd)
            _git(raw, ("merge-base", "--is-ancestor", baseline_commit,
                       first.commit))
            second = _snapshot(raw, fd)
            if first != second:
                raise DiagnosticHold("source_changed")
        finally:
            os.close(fd)
    except (OSError, SourceVersionRejected):
        raise DiagnosticHold("source_unverifiable") from None
    return GitSource(raw, baseline_commit, first.commit, first.patch)


def _recheck(source: GitSource) -> None:
    if not isinstance(source, GitSource):
        raise InvalidDiagnostic("trusted Git source required")
    if capture_git_source(source.workspace, source.baseline_commit) != source:
        raise DiagnosticHold("source_changed")


def _schema(db: sqlite3.Connection, identity: tuple[str, str]) -> None:
    app = db.execute("PRAGMA application_id").fetchone()[0]
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if (app, version) == (0, 0):
        if db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
            raise DiagnosticHold("unknown_diagnostic_database")
        with db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE identity (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                owner_id TEXT NOT NULL, project_id TEXT NOT NULL)""")
            db.execute("INSERT INTO identity VALUES (1,?,?)", identity)
            db.execute("""CREATE TABLE incidents (
                incident_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                fingerprint TEXT UNIQUE NOT NULL,
                baseline_commit TEXT NOT NULL, workspace TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('OPEN','VERIFIED_FIXED','HOLD')),
                created_at TEXT NOT NULL)""")
            db.execute("""CREATE TABLE occurrences (
                occurrence_id TEXT PRIMARY KEY,
                incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
                delivery_key TEXT UNIQUE NOT NULL, payload_hash TEXT NOT NULL,
                result_commit TEXT NOT NULL, patch_sha256 TEXT,
                kind TEXT NOT NULL, error_code TEXT NOT NULL,
                check_name TEXT NOT NULL, expected TEXT NOT NULL,
                actual TEXT NOT NULL, excerpt TEXT NOT NULL,
                created_at TEXT NOT NULL)""")
            db.execute("""CREATE TABLE attempts (
                attempt_id TEXT PRIMARY KEY,
                incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
                slot INTEGER NOT NULL CHECK(slot BETWEEN 1 AND 3),
                idempotency_key TEXT UNIQUE NOT NULL, payload_hash TEXT NOT NULL,
                hypothesis TEXT NOT NULL, test_plan TEXT NOT NULL,
                start_commit TEXT NOT NULL, start_patch TEXT,
                status TEXT NOT NULL CHECK(status IN
                    ('RESERVED','VERIFYING','FAILED','VERIFIED_FIXED','HOLD')),
                result_commit TEXT, result_patch TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(incident_id,slot))""")
            db.execute("""CREATE TABLE qa_checks (
                check_id TEXT PRIMARY KEY,
                attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
                name TEXT NOT NULL, passed INTEGER NOT NULL CHECK(passed IN (0,1)),
                source_commit TEXT NOT NULL, source_patch TEXT,
                created_at TEXT NOT NULL, UNIQUE(attempt_id,name))""")
            db.execute("""CREATE TABLE audit (
                audit_id TEXT PRIMARY KEY, action TEXT NOT NULL,
                incident_id TEXT NOT NULL, attempt_id TEXT,
                evidence_sha256 TEXT NOT NULL, created_at TEXT NOT NULL)""")
            db.execute(f"PRAGMA application_id={APPLICATION_ID}")
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    elif (app, version) != (APPLICATION_ID, SCHEMA_VERSION):
        raise DiagnosticHold("unknown_diagnostic_database")
    if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
        raise DiagnosticHold("damaged_diagnostic_database")
    if db.execute("SELECT owner_id,project_id FROM identity WHERE singleton=1").fetchone() != identity:
        raise DiagnosticHold("diagnostic_scope_mismatch")


def _audit(db: sqlite3.Connection, action: str, incident_id: str,
           attempt_id: str | None, evidence: object) -> None:
    db.execute("INSERT INTO audit VALUES (?,?,?,?,?,?)",
               ("audit_" + uuid4().hex, action, incident_id, attempt_id,
                _digest(evidence), _now()))


def _incident(db: sqlite3.Connection, incident_id: str) -> Incident:
    row = db.execute("""SELECT incident_id,task_id,fingerprint,baseline_commit,
          status FROM incidents WHERE incident_id=?""", (incident_id,)).fetchone()
    if row is None:
        raise DiagnosticHold("incident_unknown_or_other_scope")
    counts = (
        db.execute("SELECT COUNT(*) FROM occurrences WHERE incident_id=?",
                   (incident_id,)).fetchone()[0],
        db.execute("SELECT COUNT(*) FROM attempts WHERE incident_id=?",
                   (incident_id,)).fetchone()[0],
    )
    return Incident(*row, *counts)


def _attempt(db: sqlite3.Connection, attempt_id: str) -> Attempt:
    row = db.execute("""SELECT attempt_id,incident_id,slot,hypothesis,
            test_plan,status,result_commit FROM attempts WHERE attempt_id=?""",
                     (attempt_id,)).fetchone()
    if row is None:
        raise DiagnosticHold("attempt_unknown_or_other_scope")
    return Attempt(*row)


class ProjectDiagnosticsStore:
    """Approved-project offline ledger; no ability to run or authorize repairs."""

    def __init__(self, leaders: ProjectLeaderStore, *,
                 source_bindings: dict[tuple[str, str], str | Path]) -> None:
        if (not isinstance(leaders, ProjectLeaderStore)
                or type(source_bindings) is not dict or not source_bindings):
            raise InvalidDiagnostic("approved project gate and source bindings required")
        configured: dict[tuple[str, str], str] = {}
        for key, directory in source_bindings.items():
            if (type(key) is not tuple or len(key) != 2
                    or any(type(part) is not str or not _KEY.fullmatch(part)
                           for part in key)
                    or not isinstance(directory, (str, Path))):
                raise InvalidDiagnostic("invalid trusted source binding")
            raw = os.path.abspath(os.fspath(directory))
            if raw != os.path.realpath(raw):
                raise InvalidDiagnostic("symlinked source binding")
            configured[key] = raw
        if len(set(configured.values())) != len(configured):
            raise InvalidDiagnostic("one source workspace cannot own two projects")
        self._leaders = leaders
        self._bindings = MappingProxyType(configured)

    def _bound(self, scope: OwnerScope, project_id: str,
               source: GitSource) -> None:
        if isinstance(scope, OwnerScope) and project_id not in scope.project_ids:
            raise ProjectForbidden("project outside configured scope")
        if (not isinstance(scope, OwnerScope) or not isinstance(source, GitSource)
                or self._bindings.get((scope.owner_id, project_id)) != source.workspace):
            raise DiagnosticHold("source_not_bound_to_project")

    @contextmanager
    def _db(self, scope: OwnerScope, project_id: str) -> Iterator[sqlite3.Connection]:
        workspace = self._leaders.get(scope, project_id)
        if workspace is None:
            raise ProjectLeaderUnavailable("project is not approved")
        try:
            fd = os.open(workspace.project_directory,
                         os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            raise DiagnosticHold("unsafe_diagnostic_directory") from None
        try:
            _check_directory(fd)
            try:
                file_fd = _file(fd, "diagnostics_7_7.sqlite3", create=True)
            except OSError:
                raise DiagnosticHold("unsafe_diagnostic_file") from None
            os.close(file_fd)
            with closing(_database(workspace.project_directory /
                                   "diagnostics_7_7.sqlite3")) as db:
                _schema(db, (scope.owner_id, project_id))
                yield db
        finally:
            os.close(fd)

    def record_failure(self, scope: OwnerScope, project_id: str, *,
                       signal: FailureSignal, source: GitSource,
                       delivery_key: str) -> Incident:
        """Trusted adapter only: durable failure + duplicate-safe occurrence."""
        if (not isinstance(signal, FailureSignal)
                or type(signal.task_id) is not str
                or not _TASK_ID.fullmatch(signal.task_id)
                or signal.kind not in _SIGNALS):
            raise InvalidDiagnostic("invalid failure event")
        _key(delivery_key)
        _key(signal.error_code)
        _key(signal.check_name)
        _safe_text(signal.expected, 512)
        _safe_text(signal.actual, 512)
        _safe_text(signal.excerpt, MAX_EXCERPT)
        self._bound(scope, project_id, source)
        _recheck(source)
        fingerprint = _digest((scope.owner_id, project_id, signal.task_id,
                               source.baseline_commit, signal.kind,
                               signal.error_code, signal.check_name))
        payload = _digest((signal.task_id, signal.kind, signal.error_code,
                           signal.check_name, signal.expected, signal.actual,
                           signal.excerpt, source.workspace,
                           source.baseline_commit, source.result_commit,
                           source.patch_sha256))
        with self._leaders._mutex:
            with self._db(scope, project_id) as db:
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    old = db.execute("""SELECT incident_id,payload_hash
                        FROM occurrences WHERE delivery_key=?""",
                                     (delivery_key,)).fetchone()
                    if old:
                        if old[1] != payload:
                            raise DiagnosticHold("conflicting_delivery")
                        return _incident(db, old[0])
                    if db.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0] >= MAX_OCCURRENCES:
                        raise DiagnosticHold("offline_evidence_ceiling")
                    row = db.execute("SELECT incident_id,workspace,status FROM incidents WHERE fingerprint=?",
                                     (fingerprint,)).fetchone()
                    if row:
                        incident_id, stored_workspace, previous_status = row
                        if stored_workspace != source.workspace:
                            raise DiagnosticHold("source_scope_conflict")
                    else:
                        incident_id = "inc_" + uuid4().hex
                        db.execute("""INSERT INTO incidents VALUES (?,?,?,?,?,?,?)""",
                                   (incident_id, signal.task_id, fingerprint,
                                    source.baseline_commit, source.workspace,
                                    "OPEN", _now()))
                    db.execute("""INSERT INTO occurrences VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                               ("occ_" + uuid4().hex, incident_id, delivery_key,
                                payload, source.result_commit, source.patch_sha256,
                                signal.kind, signal.error_code, signal.check_name,
                                signal.expected, signal.actual, signal.excerpt, _now()))
                    if row and previous_status == "VERIFIED_FIXED":
                        # A later occurrence is a potential regression. Never
                        # silently treat a previously fixed case as resolved.
                        db.execute("""UPDATE incidents SET status='HOLD'
                            WHERE incident_id=?""", (incident_id,))
                        _audit(db, "regression_review", incident_id, None,
                               (delivery_key,source.result_commit))
                    _audit(db, "record_failure", incident_id, None, payload)
                    return _incident(db, incident_id)

    def incident(self, scope: OwnerScope, project_id: str,
                 incident_id: str) -> Incident:
        with self._leaders._mutex:
            with self._db(scope, project_id) as db:
                return _incident(db, incident_id)

    def attempts(self, scope: OwnerScope, project_id: str,
                 incident_id: str) -> tuple[Attempt, ...]:
        with self._leaders._mutex:
            with self._db(scope, project_id) as db:
                _incident(db, incident_id)
                rows = db.execute("""SELECT attempt_id FROM attempts
                    WHERE incident_id=? ORDER BY slot""", (incident_id,)).fetchall()
                return tuple(_attempt(db, row[0]) for row in rows)

    def occurrences(self, scope: OwnerScope, project_id: str,
                    incident_id: str) -> tuple[Occurrence, ...]:
        """Trusted scope-only provenance view; no external model endpoint."""
        with self._leaders._mutex:
            with self._db(scope, project_id) as db:
                _incident(db, incident_id)
                rows = db.execute("""SELECT occurrence_id,incident_id,
                    delivery_key,result_commit,patch_sha256,kind,error_code,
                    check_name,expected,actual,excerpt,created_at
                    FROM occurrences WHERE incident_id=?
                    ORDER BY created_at,occurrence_id""",
                                  (incident_id,)).fetchall()
                return tuple(Occurrence(*row) for row in rows)

    def qa_results(self, scope: OwnerScope, project_id: str,
                   attempt_id: str) -> tuple[QACheck, ...]:
        with self._leaders._mutex:
            with self._db(scope, project_id) as db:
                _attempt(db, attempt_id)
                rows = db.execute("""SELECT attempt_id,name,passed,source_commit,
                    source_patch FROM qa_checks WHERE attempt_id=?
                    ORDER BY name""", (attempt_id,)).fetchall()
                return tuple(QACheck(a, name, bool(passed), commit, patch)
                             for a, name, passed, commit, patch in rows)

    def reserve_attempt(self, scope: OwnerScope, project_id: str, *,
                        incident_id: str, source: GitSource,
                        hypothesis: str, test_plan: str,
                        idempotency_key: str) -> Attempt:
        """Reserve *before* any external effect; never executes or grants it."""
        _key(idempotency_key)
        _safe_text(hypothesis, 512)
        _safe_text(test_plan, 512)
        self._bound(scope, project_id, source)
        _recheck(source)
        payload = _digest((incident_id, source.workspace, source.baseline_commit,
                           source.result_commit, source.patch_sha256,
                           hypothesis, test_plan))
        with self._leaders._mutex:
            with self._db(scope, project_id) as db:
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    old = db.execute("""SELECT attempt_id,payload_hash
                        FROM attempts WHERE idempotency_key=?""",
                                     (idempotency_key,)).fetchone()
                    if old:
                        if old[1] != payload:
                            raise DiagnosticHold("conflicting_attempt")
                        return _attempt(db, old[0])
                    incident = _incident(db, incident_id)
                    stored = db.execute("""SELECT workspace FROM incidents
                        WHERE incident_id=?""", (incident_id,)).fetchone()[0]
                    if (incident.status != "OPEN"
                            or stored != source.workspace
                            or incident.baseline_commit != source.baseline_commit):
                        raise DiagnosticHold("attempt_not_safe")
                    prior = db.execute("""SELECT hypothesis,status FROM attempts
                        WHERE incident_id=? ORDER BY slot""",
                                       (incident_id,)).fetchall()
                    if len(prior) >= MAX_ATTEMPTS:
                        raise DiagnosticHold("attempt_limit")
                    if any(status != "FAILED" for _hyp, status in prior):
                        # Without an approved review/reopen operation, do not
                        # automatically retry a running/unknown effect.
                        raise DiagnosticHold("previous_attempt_unresolved")
                    if any(hyp.strip().casefold() == hypothesis.strip().casefold()
                           for hyp, _status in prior):
                        raise DiagnosticHold("repeated_hypothesis")
                    slot = len(prior) + 1
                    attempt_id = "att_" + uuid4().hex
                    db.execute("""INSERT INTO attempts VALUES
                        (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                               (attempt_id, incident_id, slot, idempotency_key,
                                payload, hypothesis, test_plan,
                                source.result_commit, source.patch_sha256,
                                "RESERVED", None, None, _now()))
                    _audit(db, "reserve_attempt", incident_id, attempt_id, payload)
                    return _attempt(db, attempt_id)

    def verify_attempt(self, scope: OwnerScope, project_id: str, *,
                       attempt_id: str, source: GitSource,
                       checks: dict[str, Callable[[], bool]],
                       high_risk: bool = False,
                       independent_qa: Callable[[], bool] | None = None) -> Attempt:
        """Run only trusted test callbacks; pin the exact source around them.

        This is a *test harness*, not a tool execution gateway.  Actual grants,
        live subprocesses, external model access and deployments are outside 7.7.
        """
        if (type(checks) is not dict or "reproducer" not in checks
                or not checks or len(checks) > 8 or
                any(not _KEY.fullmatch(k) or not callable(v)
                    for k, v in checks.items())
                or type(high_risk) is not bool
                or (high_risk and not callable(independent_qa))):
            raise InvalidDiagnostic("trusted QA callbacks required")
        self._bound(scope, project_id, source)
        _recheck(source)
        with self._leaders._mutex:
            with self._db(scope, project_id) as db:
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    attempt = _attempt(db, attempt_id)
                    row = db.execute("""SELECT i.workspace,i.baseline_commit,
                       a.start_commit,a.start_patch FROM attempts a
                       JOIN incidents i USING(incident_id)
                       WHERE a.attempt_id=?""", (attempt_id,)).fetchone()
                    if (attempt.status != "RESERVED"
                            or row[:2] != (source.workspace,source.baseline_commit)):
                        raise DiagnosticHold("qa_not_safe")
                    db.execute("""UPDATE attempts SET status='VERIFYING'
                        WHERE attempt_id=?""", (attempt_id,))
                    _audit(db, "qa_started", attempt.incident_id, attempt_id,
                           (source.result_commit,source.patch_sha256))
        results: list[tuple[str, bool]] = []
        uncertain = False
        try:
            for name, callback in checks.items():
                result = callback()
                results.append((name, type(result) is bool and result))
            if high_risk:
                result = independent_qa()
                results.append(("independent_qa", type(result) is bool and result))
            _recheck(source)
        except Exception:
            # Never treat an exception or changed source as a test PASS.
            results.append(("qa_interrupted", False))
            uncertain = True
        passed = all(result for _name, result in results)
        if passed and (source.result_commit, source.patch_sha256) == row[2:4]:
            # A green assertion without an identifiable result-version change
            # is insufficient evidence that an attempted code fix occurred.
            results.append(("result_unchanged", False))
            uncertain = True
            passed = False
        with self._leaders._mutex:
            with self._db(scope, project_id) as db:
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    attempt = _attempt(db, attempt_id)
                    if attempt.status != "VERIFYING":
                        raise DiagnosticHold("qa_state_changed")
                    for name, result in results:
                        db.execute("""INSERT INTO qa_checks VALUES (?,?,?,?,?,?,?)""",
                                   ("qa_" + uuid4().hex, attempt_id, name,
                                    int(result), source.result_commit,
                                    source.patch_sha256, _now()))
                    status = ("HOLD" if uncertain or
                              (high_risk and not passed) else
                              "VERIFIED_FIXED" if passed else "FAILED")
                    if status == "FAILED" and attempt.slot == MAX_ATTEMPTS:
                        status = "HOLD"
                    db.execute("""UPDATE attempts SET status=?,result_commit=?,
                        result_patch=? WHERE attempt_id=?""",
                               (status, source.result_commit,
                                source.patch_sha256, attempt_id))
                    db.execute("UPDATE incidents SET status=? WHERE incident_id=?",
                               ("OPEN" if status == "FAILED" else status,
                                attempt.incident_id))
                    _audit(db, "qa_" + status.lower(), attempt.incident_id,
                           attempt_id, (source.result_commit,results))
                    return _attempt(db, attempt_id)

    def recover_unknown(self, scope: OwnerScope, project_id: str) -> tuple[str, ...]:
        """Startup reconciliation: UNKNOWN effect -> HOLD, never blind replay."""
        held: list[str] = []
        with self._leaders._mutex:
            with self._db(scope, project_id) as db:
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    rows = db.execute("""SELECT attempt_id,incident_id FROM attempts
                        WHERE status IN ('RESERVED','VERIFYING')""").fetchall()
                    for attempt_id, incident_id in rows:
                        db.execute("""UPDATE attempts SET status='HOLD'
                            WHERE attempt_id=?""", (attempt_id,))
                        db.execute("""UPDATE incidents SET status='HOLD'
                            WHERE incident_id=?""", (incident_id,))
                        _audit(db, "recovery_unknown_hold", incident_id,
                               attempt_id, "external_effect_not_proven")
                        held.append(attempt_id)
        return tuple(held)
