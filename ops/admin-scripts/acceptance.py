"""Owner-authorized live admin, release, rollback and data-preservation test.

No provider/model calls. Evidence remains private on the existing VPS.
"""
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import urllib.request

ROOT = Path("/opt/ai-agent-engine")
DATA = Path("/var/lib/ai-agent-engine")
EVIDENCE = Path(os.environ["AE_EVIDENCE"])
SOURCE = Path.cwd()
OLD = os.environ["AE_EXPECTED_CURRENT"]
NEW = os.environ["AE_SOURCE_SHA"]


def run(*args, **kwargs):
    return subprocess.run(args, check=True, timeout=180, **kwargs)


def health():
    run("systemctl", "is-active", "--quiet", "ae-controller.service", "ae-backup.timer", "ae-host-check.timer")
    run("runuser", "-u", "ae-runtime", "--", str(ROOT / "current/venv/bin/python"),
        "-I", "-m", "ai_agent_engine.controller", "check", "--max-age", "15")


def unchanged_main():
    req = urllib.request.Request(
        "https://api.github.com/repos/vl-ktk/ai-agent-engine/git/ref/heads/main",
        headers={"User-Agent": "ai-agent-engine-owner-admin", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as response:
        assert json.load(response)["object"]["sha"] == os.environ["AE_REQUEST_SHA"], "GitHub main changed"


def inventory(label, save=False):
    records = []
    for folder in ("projects", "workspaces", "artifacts"):
        base = DATA / folder
        for file in sorted(base.rglob("*")):
            assert not file.is_symlink(), "unexpected symlink in working data"
            if not file.is_file() or file.name.endswith(("-wal", "-shm")):
                continue
            relative = file.relative_to(DATA)
            if file.suffix in (".sqlite3", ".sqlite", ".db"):
                with sqlite3.connect(file.as_uri() + "?mode=ro", uri=True) as db:
                    assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
                    content = "\n".join(db.iterdump()).encode()
                    if save:
                        backup_path = EVIDENCE / "data-before" / relative
                        backup_path.parent.mkdir(parents=True, exist_ok=True)
                        with sqlite3.connect(backup_path) as backup:
                            db.backup(backup)
            else:
                content = file.read_bytes()
                if save:
                    backup_path = EVIDENCE / "data-before" / relative
                    backup_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(file, backup_path)
            records.append((str(relative), len(content), hashlib.sha256(content).hexdigest()))
    raw = json.dumps(records, sort_keys=True).encode()
    (EVIDENCE / (label + ".json")).write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    print(f"PASS: data inventory {label} files={len(records)} sha256={digest}", flush=True)
    return records


def main():
    assert os.getuid() == 0
    assert os.readlink(ROOT / "current") == "releases/" + OLD
    assert OLD != NEW
    (EVIDENCE / "rollback-release.txt").write_text(OLD + "\n")
    run("id")
    failed = ROOT / "releases/4c5cf0903ca17c865ad38d85bcc6540f747017e0"
    if failed.exists():
        for path in (failed / "venv", failed / "venv/lib"):
            print(f"DIAG previous failed stage {path.name} mode={oct(path.stat().st_mode & 0o777)}", flush=True)
    run("journalctl", "-u", "ae-controller.service", "-n", "20", "--no-pager", "-o", "cat")
    run("visudo", "-c")
    ssh = subprocess.check_output(["/usr/sbin/sshd", "-T"], text=True)
    for expected in ("permitrootlogin no", "passwordauthentication no", "kbdinteractiveauthentication no"):
        assert expected in ssh.splitlines(), expected
    print("PASS: root SSH/password SSH remain disabled", flush=True)
    health()
    before = inventory("before", save=True)
    run("runuser", "-u", "ae-runtime", "--", "python3", "/usr/local/libexec/ae-backup.py", "verify")
    print("PASS: baseline recorded before administrative smoke", flush=True)

    # A real installation from the existing Debian repositories; no paid service.
    run("apt-get", "update", "-qq")
    run("apt-get", "install", "--no-install-recommends", "-y", "hello")
    run("/usr/bin/hello")
    marker = EVIDENCE / "root-smoke.txt"
    marker.write_text("owner-authorized cloud root smoke\n")
    assert marker.stat().st_uid == 0
    run("systemd-run", "--wait", "--collect", "--unit=ae-admin-smoke-" + os.environ["AE_REQUEST_ID"], "/usr/bin/true")
    print("PASS: package installation, root file and transient service", flush=True)

    spec = importlib.util.spec_from_file_location("promotion", SOURCE / "infra/deploy/promotion.py")
    promotion = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(promotion)
    bundle = SOURCE / "bundle"
    manifest = json.loads((bundle / "release-manifest.json").read_text())
    digest = manifest["wheel_sha256"]
    promotion.stage_release(bundle, ROOT, NEW, expected_wheel_sha256=digest, interpreter="/usr/bin/python3")
    assert promotion.active_sha(ROOT) == OLD, "concurrent deployment"
    run("runuser", "-u", "ae-runtime", "--", str(ROOT / "releases" / NEW / "venv/bin/python"),
        "-I", "-c", "from ai_agent_engine.controller import check_health; print('PASS: staged code readable as ae-runtime')")

    # Compare-and-swap publication. Every failed health check returns to OLD.
    promoted = False
    try:
        unchanged_main()
        promotion.promote_release(ROOT, NEW, expected_current=OLD)
        promoted = True
        run("systemctl", "restart", "ae-controller.service")
        health()
        assert inventory("after-deploy") == before
        print("PASS: new release live and working data preserved", flush=True)
        unchanged_main()
        promotion.promote_release(ROOT, OLD, expected_current=NEW)
        promoted = False
        run("systemctl", "restart", "ae-controller.service")
        health()
        assert inventory("after-rollback") == before
        print("PASS: actual rollback to previous release and health", flush=True)
        unchanged_main()
        promotion.promote_release(ROOT, NEW, expected_current=OLD)
        promoted = True
        run("systemctl", "restart", "ae-controller.service")
        health()
        assert inventory("final") == before
        run("runuser", "-u", "ae-runtime", "--", "python3", "/usr/local/libexec/ae-backup.py", "verify")
    except BaseException:
        if promoted and promotion.active_sha(ROOT) == NEW:
            promotion.promote_release(ROOT, OLD, expected_current=NEW)
            run("systemctl", "restart", "ae-controller.service")
            health()
            print("RECOVERED: returned to previous release after failed acceptance", flush=True)
        raise
    installed = subprocess.check_output([str(ROOT / "current/venv/bin/python"), "-I", "-c",
        "import importlib.metadata; print(importlib.metadata.version('ai-agent-engine'))"], text=True).strip()
    result = {"status": "PASS", "installed": NEW, "previous": OLD, "version": installed,
              "wheel_sha256": digest, "request": os.environ["AE_REQUEST_ID"], "data_files": len(before)}
    (EVIDENCE / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print("LIVE_ACCEPTANCE " + json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
