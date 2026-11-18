"""Trusted owner-admin bootstrap streamed from the verified Actions checkout."""
import argparse
import fcntl
import hashlib
import os
from pathlib import Path
import re
import subprocess
import tarfile


def main():
    p = argparse.ArgumentParser()
    for name in ("request-id", "request-sha", "source-sha", "expected-current", "payload-sha", "script", "script-sha256"):
        p.add_argument("--" + name, required=True)
    a = p.parse_args()
    for value, length in ((a.request_id, 32), (a.request_sha, 40), (a.source_sha, 40), (a.expected_current, 40),
                          (a.payload_sha, 64), (a.script_sha256, 64)):
        if not re.fullmatch(r"[0-9a-f]{%d}" % length, value):
            raise ValueError("invalid immutable identifier")
    if not re.fullmatch(r"ops/admin-scripts/[a-z0-9_-]{1,64}\.py", a.script):
        raise ValueError("invalid reviewed script path")
    assert os.getuid() == 0
    os.umask(0o077)
    with open("/run/lock/ae-owner-admin.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert os.readlink("/opt/ai-agent-engine/current") == "releases/" + a.expected_current, "VPS changed"
        incoming = Path("/home/ae-deploy/ae-admin-incoming") / (a.request_id + ".tar")
        assert not incoming.is_symlink() and incoming.stat().st_size < 50_000_000
        payload = incoming.read_bytes()
        assert hashlib.sha256(payload).hexdigest() == a.payload_sha, "payload changed"
        work = Path("/root/ae-admin-jobs") / a.request_id
        work.mkdir(parents=True, exist_ok=False)
        archive = work / "payload.tar"
        archive.write_bytes(payload)
        source = work / "source"
        source.mkdir()
        with tarfile.open(archive) as bundle:
            members = bundle.getmembers()
            assert sum(m.size for m in members) < 100_000_000
            assert all((m.isfile() or m.isdir()) and not Path(m.name).is_absolute()
                       and ".." not in Path(m.name).parts for m in members)
            bundle.extractall(source, filter="data")
        script = source / a.script
        assert hashlib.sha256(script.read_bytes()).hexdigest() == a.script_sha256
        environment = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8",
                       "AE_SOURCE_SHA": a.source_sha, "AE_REQUEST_SHA": a.request_sha,
                       "AE_EXPECTED_CURRENT": a.expected_current,
                       "AE_REQUEST_ID": a.request_id, "AE_EVIDENCE": str(work / "evidence"),
                       "DEBIAN_FRONTEND": "noninteractive"}
        Path(environment["AE_EVIDENCE"]).mkdir()
        def log(status):
            subprocess.run(["logger", "-t", "ae-owner-admin",
                            f"request={a.request_id} source={a.source_sha} status={status}"], check=True)
        log("started")
        result = subprocess.run(["/usr/bin/python3", "-I", "-u", str(script)],
                                cwd=source, env=environment, timeout=900)
        log(str(result.returncode))
        raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
