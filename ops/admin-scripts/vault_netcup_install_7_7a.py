#!/usr/bin/env python3
"""Install a NEW isolated Secure Vault service on Netcup via owner GitOps.

Never changes the installed AE controller, release operator, other project,
existing credentials or legacy service. The reviewed admin wrapper checks the
exact controller CAS and the SHA-256 of this script before running as root.
No encryption keys are printed, archived, committed or written unencrypted.
"""

from __future__ import annotations

import grp
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import socket
import stat
import subprocess
import sys
import time


BASELINE = "0e04af007375d82daf8f7d3b36c71abb0c784e0b"
CONTROLLER = Path("/opt/ai-agent-engine/current")
RELEASE_ROOT = Path("/opt/ai-agent-engine-vault/releases")
UNIT_ROOT = Path("/etc/systemd/system")
CREDENTIAL_ROOT = Path("/etc/ai-agent-engine/vault")
BACKUP_ROOT = Path("/var/backups/ai-agent-engine-vault")
SOCKET_PATH = Path("/run/ae-vault.sock")
UNITS = (
    "ae-vault.socket", "ae-vault.service", "ae-vault-backup.service",
    "ae-vault-backup.timer",
)


def _run(args: list[str], *, input_data: bytes | None = None, timeout: int = 30) -> None:
    result = subprocess.run(args, input=input_data, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=timeout,
                            check=False)
    if result.returncode:
        raise RuntimeError("host command failed")


def _expected_controller() -> None:
    if (os.getenv("AE_EXPECTED_CURRENT") != BASELINE
            or os.readlink(CONTROLLER) != "releases/" + BASELINE):
        raise RuntimeError("controller baseline changed")


def _new_private_dir(path: Path, *, uid: int, gid: int) -> None:
    path.mkdir(mode=0o700, parents=False, exist_ok=False)
    os.chown(path, uid, gid)
    os.chmod(path, 0o700)


def _write_new(path: Path, data: bytes, *, mode: int = 0o644,
               uid: int = 0, gid: int = 0) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 mode)
    with os.fdopen(fd, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    os.chown(path, uid, gid)
    os.chmod(path, mode)


def _socket_request(operation: str, **values: str) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(10)
        conn.connect(str(SOCKET_PATH))
        conn.sendall(json.dumps({"operation": operation, **values},
                                separators=(",", ":")).encode() + b"\n")
        data = bytearray()
        while not data.endswith(b"\n") and len(data) <= 4096:
            next_byte = conn.recv(1)
            if not next_byte:
                break
            data += next_byte
    if not data.endswith(b"\n") or len(data) > 4096:
        raise RuntimeError("Vault health response missing")
    parsed = json.loads(data)
    if type(parsed) is not dict or parsed.get("ok") is not True:
        raise RuntimeError("Vault health response denied")
    return parsed


def _unit_specs(sha: str, worker_group: str) -> dict[str, bytes]:
    launcher = f"/opt/ai-agent-engine-vault/releases/{sha}/run.py"
    service = f"""[Unit]
Description=AI Agent Engine isolated Secure Vault 7.7A
Requires=ae-vault.socket
After=ae-vault.socket

[Service]
Type=simple
User=ae-vault
Group=ae-vault
ExecStart=/usr/bin/python3 -I {launcher} serve
StateDirectory=ai-agent-engine-vault
StateDirectoryMode=0700
RuntimeDirectory=ai-agent-engine-vault
RuntimeDirectoryMode=0700
LoadCredentialEncrypted=vault-master:/etc/ai-agent-engine/vault/master.cred
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
RestrictAddressFamilies=AF_UNIX
CapabilityBoundingSet=
UMask=0077
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""
    backup = f"""[Unit]
Description=AI Agent Engine Secure Vault verified encrypted local backup
After=ae-vault.service

[Service]
Type=oneshot
User=ae-vault
Group=ae-vault
ExecStart=/usr/bin/python3 -I {launcher} backup
StateDirectory=ai-agent-engine-vault
StateDirectoryMode=0700
RuntimeDirectory=ai-agent-engine-vault
RuntimeDirectoryMode=0700
LoadCredentialEncrypted=vault-master:/etc/ai-agent-engine/vault/master.cred
LoadCredentialEncrypted=vault-backup:/etc/ai-agent-engine/vault/backup.cred
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/var/backups/ai-agent-engine-vault
ProtectHome=yes
PrivateTmp=yes
RestrictAddressFamilies=AF_UNIX
CapabilityBoundingSet=
UMask=0077
"""
    unit_files = {
        "ae-vault.socket": f"""[Unit]
Description=Local-only AF_UNIX Secure Vault endpoint

[Socket]
ListenStream=/run/ae-vault.sock
SocketUser=ae-vault
SocketGroup={worker_group}
SocketMode=0660
RemoveOnStop=yes

[Install]
WantedBy=sockets.target
""",
        "ae-vault.service": service,
        "ae-vault-backup.service": backup,
        "ae-vault-backup.timer": """[Unit]
Description=Daily local encrypted Secure Vault snapshots (NOT off-host recovery)

[Timer]
OnCalendar=daily
Persistent=true
RandomizedDelaySec=5m
Unit=ae-vault-backup.service

[Install]
WantedBy=timers.target
""",
    }
    return {name: contents.encode("utf-8") for name, contents in unit_files.items()}


def main() -> int:
    os.umask(0o077)
    if os.geteuid() != 0:
        raise RuntimeError("requires owner admin root")
    sha = os.getenv("AE_SOURCE_SHA", "")
    request_id = os.getenv("AE_REQUEST_ID", "")
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or not re.fullmatch(r"[0-9a-f]{32}", request_id):
        raise RuntimeError("invalid reviewed source")
    _expected_controller()
    source = Path.cwd()
    if source.name != "source" or source.parent.name != request_id:
        raise RuntimeError("unexpected owner-admin source directory")
    for relative in ("ai_agent_engine/secure_vault.py", "ai_agent_engine/vault_runtime.py"):
        file = source / relative
        if file.is_symlink() or not file.is_file() or file.stat().st_size > 150_000:
            raise RuntimeError("invalid reviewed module")

    # Entire dependency/host/collision preflight before creating any file.
    if sys.version_info < (3, 11) or sys.executable != "/usr/bin/python3":
        raise RuntimeError("unsupported Python interpreter")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
    if shutil.which("systemd-creds") is None:
        raise RuntimeError("systemd-creds unavailable")
    worker = pwd.getpwnam("ae-runtime")
    worker_group = grp.getgrgid(worker.pw_gid).gr_name
    if (worker.pw_uid == 0 or worker_group != "ae-runtime"
            or grp.getgrnam(worker_group).gr_mem):
        raise RuntimeError("unsafe worker identity")
    try:
        pwd.getpwnam("ae-vault")
    except KeyError:
        pass
    else:
        raise RuntimeError("Vault service account already exists")
    release = RELEASE_ROOT / sha
    if any(path.exists() or path.is_symlink() for path in (
        release, BACKUP_ROOT, CREDENTIAL_ROOT, SOCKET_PATH,
        *(UNIT_ROOT / item for item in UNITS),
    )):
        raise RuntimeError("Vault installation target exists; refuse overwrite")
    for parent in (Path("/opt"), Path("/opt/ai-agent-engine-vault"),
                   RELEASE_ROOT, Path("/etc/ai-agent-engine"),
                   CREDENTIAL_ROOT.parent, Path("/var/backups"), UNIT_ROOT):
        if parent.exists():
            st = parent.lstat()
            if (not stat.S_ISDIR(st.st_mode) or st.st_uid != 0
                    or stat.S_IMODE(st.st_mode) & 0o022):
                raise RuntimeError("unsafe existing parent")
    _expected_controller()

    # Only new, isolated Vault-scoped paths are created; no old units touched.
    _run(["/usr/sbin/useradd", "--system", "--user-group", "--no-create-home",
          "--home-dir", "/nonexistent", "--shell", "/usr/sbin/nologin", "ae-vault"])
    vault_user = pwd.getpwnam("ae-vault")
    RELEASE_ROOT.mkdir(parents=True, mode=0o755, exist_ok=True)
    # umask 077 intentionally protects credentials, but code directories must
    # remain traversable by the dedicated unprivileged service account.
    for public_code_parent in (RELEASE_ROOT.parent, RELEASE_ROOT):
        os.chmod(public_code_parent, 0o755)
    release.mkdir(mode=0o755, exist_ok=False)
    os.chmod(release, 0o755)
    package = release / "ai_agent_engine"
    package.mkdir(mode=0o755)
    os.chmod(package, 0o755)
    _write_new(package / "__init__.py", b'"""Private Netcup Vault adapter."""\n')
    for filename in ("secure_vault.py", "vault_runtime.py"):
        _write_new(package / filename, (source / "ai_agent_engine" / filename).read_bytes())
    launcher = (b"import sys\nfrom pathlib import Path\n"
                b"sys.path.insert(0, str(Path(__file__).resolve().parent))\n"
                b"from ai_agent_engine.vault_runtime import main\n"
                b"raise SystemExit(main())\n")
    _write_new(release / "run.py", launcher)
    _new_private_dir(BACKUP_ROOT, uid=vault_user.pw_uid, gid=vault_user.pw_gid)
    CREDENTIAL_ROOT.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _new_private_dir(CREDENTIAL_ROOT, uid=0, gid=0)
    for credential_name, filename in (("vault-master", "master.cred"),
                                      ("vault-backup", "backup.cred")):
        file = CREDENTIAL_ROOT / filename
        _run(["/usr/bin/systemd-creds", "encrypt", "--with-key=host",
              "--name=" + credential_name, "-", str(file)],
             input_data=os.urandom(32))
        os.chown(file, 0, 0)
        os.chmod(file, 0o600)
    for filename, data in _unit_specs(sha, worker_group).items():
        _write_new(UNIT_ROOT / filename, data)

    # Immediate CAS again: never activate under an unexpected core revision.
    _expected_controller()
    _run(["/usr/bin/systemctl", "daemon-reload"])
    _run(["/usr/bin/systemctl", "enable", "--now", "ae-vault.socket"])
    _run(["/usr/bin/systemctl", "start", "ae-vault.service"], timeout=60)
    for _ in range(20):
        try:
            state = _socket_request("status")
            if state.get("status") == "ready":
                break
        except (OSError, ValueError, RuntimeError):
            time.sleep(0.25)
    else:
        raise RuntimeError("Vault did not become ready")
    _socket_request("ensure_project", project_id="ai-agent-engine")
    _run(["/usr/bin/systemctl", "start", "ae-vault-backup.service"], timeout=120)
    _run(["/usr/bin/systemctl", "enable", "--now", "ae-vault-backup.timer"])
    for unit in ("ae-vault.socket", "ae-vault.service", "ae-vault-backup.timer"):
        _run(["/usr/bin/systemctl", "is-active", "--quiet", unit])
    copies = list(BACKUP_ROOT.glob("vault-*.aev"))
    if len(copies) != 1 or (copies[0].stat().st_mode & 0o777) != 0o600:
        raise RuntimeError("first encrypted restore-checked backup not found")
    _expected_controller()
    print("VAULT_INSTALL=" + json.dumps({
        "status": "ACTIVE_LOCAL_ONLY", "source_sha": sha,
        "controller_unchanged": BASELINE, "socket": "ae-vault.socket",
        "service": "ae-vault.service", "backup_timer": "ae-vault-backup.timer",
        "first_backup": "ENCRYPTED_AND_RESTORE_TESTED", "off_host_recovery": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, KeyError, ImportError, subprocess.TimeoutExpired):
        print("VAULT_INSTALL=HOLD_OR_FAILED_CHECK_SERVICE_STATE")
        raise SystemExit(1)
