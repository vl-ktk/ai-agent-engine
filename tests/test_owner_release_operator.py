"""Offline functional tests of fail-closed Netcup release activation."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from infra.deploy.owner_release_operator import ReleaseRefused, perform

OLD = "a" * 40
NEW = "b" * 40
DIGEST = "c" * 64


class FakePromotion:
    def __init__(self):
        self.current = OLD
        self.staged = False
        self.calls = []

    def active_sha(self, root):
        self.calls.append("read")
        return self.current

    def stage_release(self, bundle, root, sha, *, expected_wheel_sha256, interpreter):
        self.calls.append("stage")
        assert sha == NEW and expected_wheel_sha256 == DIGEST
        self.staged = True

    def promote_release(self, root, sha, *, expected_current):
        self.calls.append("promote")
        assert self.current == expected_current
        self.current = sha


class OperatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "ai-agent-engine"
        (self.root / "releases" / OLD).mkdir(parents=True)
        self.incoming = Path(self.tmp.name) / "incoming"
        (self.incoming / NEW).mkdir(parents=True)
        self.p = FakePromotion()

    def go(self, operation="activate", source=NEW, expected=OLD,
           digest=DIGEST, restart=lambda: None):
        return perform(self.p, operation=operation, source_sha=source,
                       expected_current=expected, wheel_sha256=digest,
                       root=self.root, incoming=self.incoming, restart=restart)

    def test_activate_stages_switches_and_checks(self):
        checks = []
        self.go(restart=lambda: checks.append("health"))
        self.assertEqual(self.p.current, NEW)
        self.assertEqual(self.p.calls, ["read", "stage", "promote"])
        self.assertEqual(checks, ["health"])

    def test_refuse_changed_current(self):
        self.p.current = "d" * 40
        with self.assertRaises(ReleaseRefused):
            self.go()
        self.assertNotIn("promote", self.p.calls)

    def test_refuse_invalid_hash(self):
        with self.assertRaises(ReleaseRefused):
            self.go(digest="wrong")
        self.assertNotIn("stage", self.p.calls)

    def test_auto_restore_after_failed_health(self):
        count = [0]
        def health():
            count[0] += 1
            if count[0] == 1:
                raise RuntimeError("unhealthy")
        with self.assertRaisesRegex(ReleaseRefused, "previous release restored"):
            self.go(restart=health)
        self.assertEqual(self.p.current, OLD)
        self.assertEqual(count[0], 2)
        self.assertEqual(self.p.calls.count("promote"), 2)

    def test_fail_closed_when_rollback_also_fails(self):
        def unhealthy():
            raise RuntimeError("service broken")
        with self.assertRaisesRegex(ReleaseRefused, "manual recovery required"):
            self.go(restart=unhealthy)

    def test_rollback_existing_release(self):
        (self.root / "releases" / NEW).mkdir()
        self.p.current = NEW
        self.go(operation="rollback", source=OLD, expected=NEW, digest="ignored")
        self.assertEqual(self.p.current, OLD)
        self.assertNotIn("stage", self.p.calls)

    def test_reject_unknown_operation(self):
        with self.assertRaises(ReleaseRefused):
            self.go(operation="shell")
        self.assertNotIn("promote", self.p.calls)


if __name__ == "__main__":
    unittest.main()
