"""Offline adversarial tests of Netcup release activation and SSH signatures."""
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import time
import unittest

from infra.deploy.owner_release_operator import (
    ReleaseRefused, perform, trusted_bundle, verify_owner_intent,
)

OLD = "a" * 40
NEW = "b" * 40
DIGEST = "c" * 64
NS = "ai-agent-engine-release@vl-ktk"


class FakePromotion:
    def __init__(self):
        self.current = OLD
        self.calls = []

    def active_sha(self, root):
        self.calls.append("read")
        return self.current

    def stage_release(self, bundle, root, sha, *, expected_wheel_sha256, interpreter):
        self.calls.append("stage")
        assert sha == NEW and expected_wheel_sha256 == DIGEST

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
        self.authorized = []

    def go(self, operation="activate", source=NEW, expected=OLD,
           digest=DIGEST, restart=lambda: None, *, authorize=True):
        def check(**kwargs):
            self.authorized.append(kwargs)
        return perform(self.p, operation=operation, source_sha=source,
                       expected_current=expected, wheel_sha256=digest,
                       root=self.root, incoming=self.incoming, restart=restart,
                       authorize=check if authorize else verify_owner_intent,
                       bundle_factory=lambda **kw: nullcontext(self.incoming / NEW))

    def test_activate_stages_switches_and_checks(self):
        checks = []
        self.go(restart=lambda: checks.append("health"))
        self.assertEqual(self.p.current, NEW)
        self.assertEqual(self.p.calls, ["read", "stage", "promote"])
        self.assertEqual(checks, ["health"])
        self.assertEqual(self.authorized[0]["target"], NEW)

    def test_refuse_changed_current_before_authorization(self):
        self.p.current = "d" * 40
        with self.assertRaises(ReleaseRefused):
            self.go()
        self.assertEqual(self.authorized, [])
        self.assertNotIn("promote", self.p.calls)

    def test_refuse_invalid_hash_before_authorization(self):
        with self.assertRaises(ReleaseRefused):
            self.go(digest="wrong")
        self.assertEqual(self.authorized, [])
        self.assertNotIn("stage", self.p.calls)

    def test_unsigned_release_fails_closed(self):
        with self.assertRaises((ReleaseRefused, OSError)):
            self.go(authorize=False)
        self.assertNotIn("stage", self.p.calls)
        self.assertNotIn("promote", self.p.calls)

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

    def test_explicit_rollback_requires_authorization(self):
        (self.root / "releases" / NEW).mkdir()
        self.p.current = NEW
        self.go(operation="rollback", source=OLD, expected=NEW, digest="0" * 64)
        self.assertEqual(self.p.current, OLD)
        self.assertEqual(self.authorized[0]["operation"], "rollback")
        self.assertNotIn("stage", self.p.calls)

    def test_reject_unknown_operation(self):
        with self.assertRaises(ReleaseRefused):
            self.go(operation="shell")
        self.assertNotIn("promote", self.p.calls)


@unittest.skipUnless(os.name == "posix" and shutil.which("ssh-keygen"),
                     "requires POSIX and OpenSSH ssh-keygen")
class SignedIntentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "ai-agent-engine"
        (self.root / "releases").mkdir(parents=True)
        self.incoming = Path(self.tmp.name) / "incoming"
        self.source = self.incoming / NEW
        self.source.mkdir(parents=True)
        self.key = Path(self.tmp.name) / "owner-key"
        self.signers = Path(self.tmp.name) / "allowed_signers"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "",
                        "-f", str(self.key)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.signers.write_text(
            "ai-agent-engine-owner " + self.key.with_suffix(".pub").read_text())
        self.now = int(time.time())

    def sign(self, *, operation="activate", source_sha=NEW, digest=DIGEST,
             issue_offset=0, expires_offset=900, mutate=False):
        payload = {"schema": 1, "operation": operation, "source_sha": source_sha,
                   "expected_current": OLD, "wheel_sha256": digest,
                   "issued_at": self.now + issue_offset,
                   "expires_at": self.now + expires_offset}
        f = self.source / "release-intent.json"
        f.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n")
        subprocess.run(["ssh-keygen", "-Y", "sign", "-f", str(self.key),
                        "-n", NS, str(f)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if mutate:
            f.write_text(f.read_text().replace("activate", "rollback"))

    def verify(self, *, digest=DIGEST, operation="activate"):
        return verify_owner_intent(
            operation=operation, target=NEW, previous=OLD, digest=digest,
            incoming=self.incoming, root=self.root,
            signers=self.signers, owner_uid=os.getuid(), now=self.now,
        )

    def test_signed_intent_accepted(self):
        self.sign()
        self.verify()

    def test_missing_signature_is_refused(self):
        self.sign()
        (self.source / "release-intent.json.sig").unlink()
        with self.assertRaises((ReleaseRefused, OSError)):
            self.verify()

    def test_mutation_after_signing_is_refused(self):
        self.sign(mutate=True)
        with self.assertRaises(ReleaseRefused):
            self.verify()

    def test_wrong_digest_is_refused(self):
        self.sign()
        with self.assertRaises(ReleaseRefused):
            self.verify(digest="e" * 64)

    def test_expired_intent_is_refused(self):
        self.sign(issue_offset=-1800, expires_offset=-100)
        with self.assertRaises(ReleaseRefused):
            self.verify()

    def test_signed_intent_with_wrong_signer_is_refused(self):
        self.sign()
        self.signers.write_text("other-principal " +
                                self.key.with_suffix(".pub").read_text())
        with self.assertRaises(ReleaseRefused):
            self.verify()

    def test_signer_file_writable_by_others_is_refused(self):
        self.sign()
        self.signers.chmod(0o666)
        with self.assertRaises(ReleaseRefused):
            self.verify()

    def test_signature_symlink_is_refused(self):
        self.sign()
        sig = self.source / "release-intent.json.sig"
        dest = self.source / "copy.sig"
        sig.rename(dest)
        sig.symlink_to(dest)
        with self.assertRaises((ReleaseRefused, OSError)):
            self.verify()


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "ai-agent-engine"
        (self.root / "releases").mkdir(parents=True)
        self.incoming = Path(self.tmp.name) / "incoming"
        self.source = self.incoming / NEW
        self.source.mkdir(parents=True)
        self.wheel_bytes = b"wheel-content-not-executable"
        self.digest = hashlib.sha256(self.wheel_bytes).hexdigest()
        self.name = "ai_agent_engine-1.0-py3-none-any.whl"
        (self.source / self.name).write_bytes(self.wheel_bytes)
        (self.source / "release-manifest.json").write_text(
            json.dumps({"wheel_file": self.name, "source_commit": NEW}))

    def test_snapshot_uses_immutable_copy_not_incoming(self):
        with trusted_bundle(incoming=self.incoming, target=NEW,
                            digest=self.digest, root=self.root) as bundle:
            (self.source / self.name).write_bytes(b"attacker swaps wheel")
            self.assertEqual((bundle / self.name).read_bytes(), self.wheel_bytes)
        self.assertFalse(bundle.exists())

    def test_reject_mismatched_signed_digest(self):
        with self.assertRaises(ReleaseRefused):
            with trusted_bundle(incoming=self.incoming, target=NEW,
                                digest="c" * 64, root=self.root):
                pass

    def test_reject_symlinked_wheel(self):
        f = self.source / self.name
        target = self.source / "actual.whl"
        f.rename(target)
        f.symlink_to(target)
        with self.assertRaises((ReleaseRefused, OSError)):
            with trusted_bundle(incoming=self.incoming, target=NEW,
                                digest=self.digest, root=self.root):
                pass

    def test_reject_hardlinked_wheel(self):
        f = self.source / self.name
        os.link(f, self.source / "second-link.whl")
        with self.assertRaises(ReleaseRefused):
            with trusted_bundle(incoming=self.incoming, target=NEW,
                                digest=self.digest, root=self.root):
                pass


if __name__ == "__main__":
    unittest.main()
