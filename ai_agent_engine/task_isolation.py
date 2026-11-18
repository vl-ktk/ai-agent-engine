"""Stage 6.8: fail-closed Linux command sandbox and secret-safe output.

This is for offline task commands only. Networked GPT/Claude subscription
workers have separate, credential-bearing processes and are not silently
rerouted into this offline sandbox.
"""

from __future__ import annotations

import ctypes
import errno
import os
import re
import shutil
import stat
import subprocess
from typing import Mapping


class IsolationUnavailable(RuntimeError):
    """Required mount/PID/user or syscall boundary cannot be installed."""


REDACTED = "[REDACTED]"
_SENSITIVE_NAME = re.compile(
    r"(?:^|_)(?:API_KEY|ACCESS_KEY|PRIVATE_KEY|TOKEN|PASSWORD|SECRET|"
    r"CREDENTIAL|AUTHORIZATION)(?:$|_)", re.IGNORECASE,
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b([a-z0-9_]*(?:api[_-]?key|token|password|secret|"
    r"authorization)[a-z0-9_]*\s*[:=]\s*)([^\s,;'\"]{8,})"
)
_CREDENTIAL_PATTERN = re.compile(
    r"(?i)\b(?:sk-(?:proj-)?[a-z0-9_-]{20,}|"
    r"gh[pousr]_[a-z0-9]{25,}|github_pat_[a-z0-9_]{25,}|"
    r"xox[baprs]-[a-z0-9-]{24,})\b"
)
_BEARER_PATTERN = re.compile(
    r"(?i)\b((?:Bearer|Basic)\s+)([a-z0-9._~+/-]{12,})"
)


def sensitive_environment_values(environment: Mapping[str, str]) -> tuple[str, ...]:
    """Snapshot literal secrets for masking; never copy them into children."""
    if not isinstance(environment, Mapping):
        raise TypeError("trusted environment mapping required")
    return tuple(sorted({value for name, value in environment.items()
                         if type(name) is str and _SENSITIVE_NAME.search(name)
                         and type(value) is str and len(value) >= 8},
                        key=len, reverse=True))


def redact_text(value: str, *, secrets: tuple[str, ...] = ()) -> str:
    """Mask known credentials and high-confidence token shapes in output."""
    if type(value) is not str:
        raise TypeError("text required")
    for secret in secrets:
        if type(secret) is str and len(secret) >= 8:
            value = value.replace(secret, REDACTED)
    value = _CREDENTIAL_PATTERN.sub(REDACTED, value)
    value = _BEARER_PATTERN.sub(lambda m: m.group(1) + REDACTED, value)
    return _SECRET_ASSIGNMENT.sub(lambda m: m.group(1) + REDACTED, value)


def provider_worker_environment(source: Mapping[str, str], package_root: str) -> dict[str, str]:
    """Minimal trusted service env; OAuth stays in the provider's own HOME.

    Unlike offline sandbox commands, a real GPT/Claude subprocess needs its
    personal login and network. Do not inherit arbitrary parent environment
    variables or expose unrelated credentials to the provider process.
    """
    selected = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "TERM",
                "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "SSL_CERT_FILE",
                "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
                "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
                "http_proxy", "https_proxy", "no_proxy")
    result = {name: source[name] for name in selected
              if type(source.get(name)) is str and "\x00" not in source[name]}
    # PYTHONPATH comes exclusively from installed source, not parent env.
    result["PYTHONPATH"] = package_root
    result["PYTHONNOUSERSITE"] = "1"
    result["PYTHONDONTWRITEBYTECODE"] = "1"
    return result


_BLOCKED_SYSCALLS = (
    # In this host unshare-net cannot initialize loopback. A kernel-enforced
    # seccomp filter prevents creation/use of any network sockets instead.
    "socket", "socketpair", "connect", "bind", "listen", "accept",
    "accept4", "sendto", "sendmsg", "sendmmsg", "recvfrom", "recvmsg",
    "recvmmsg", "setsockopt", "getsockopt", "shutdown", "socketcall",
    # Keep the sandbox from escaping via namespaces, host introspection or
    # kernel interfaces; Bubblewrap also disables nested user namespaces.
    "ptrace", "process_vm_readv", "process_vm_writev", "bpf",
    "perf_event_open", "keyctl", "add_key", "request_key",
    "mount", "umount2", "pivot_root", "open_by_handle_at", "setns",
    "unshare", "init_module", "finit_module", "delete_module",
    "kexec_load", "io_uring_setup",
)


def _network_and_escape_filter_fd() -> int:
    """Export a libseccomp BPF filter as a private FD for Bubblewrap."""
    if not hasattr(os, "memfd_create"):
        raise IsolationUnavailable("kernel security filter unavailable")
    try:
        seccomp = ctypes.CDLL("libseccomp.so.2")
        seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
        seccomp.seccomp_init.restype = ctypes.c_void_p
        seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
        seccomp.seccomp_syscall_resolve_name.restype = ctypes.c_int
        seccomp.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                             ctypes.c_int, ctypes.c_uint]
        seccomp.seccomp_rule_add.restype = ctypes.c_int
        seccomp.seccomp_export_bpf.argtypes = [ctypes.c_void_p, ctypes.c_int]
        seccomp.seccomp_export_bpf.restype = ctypes.c_int
        seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
        seccomp.seccomp_release.restype = None
        ctx = seccomp.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
        if not ctx:
            raise IsolationUnavailable("security filter initialization failed")
        try:
            for name in _BLOCKED_SYSCALLS:
                number = seccomp.seccomp_syscall_resolve_name(name.encode("ascii"))
                if number < 0:
                    if name in ("socket", "connect", "mount", "ptrace"):
                        raise IsolationUnavailable("required syscall rule unavailable")
                    continue
                action = 0x00050000 | errno.EPERM  # SCMP_ACT_ERRNO(EPERM)
                if seccomp.seccomp_rule_add(ctx, action, number, 0) != 0:
                    raise IsolationUnavailable("security rule installation failed")
            fd = os.memfd_create("ae-sandbox-filter", flags=os.MFD_CLOEXEC)
            try:
                if seccomp.seccomp_export_bpf(ctx, fd) != 0:
                    raise IsolationUnavailable("security filter export failed")
                os.lseek(fd, 0, os.SEEK_SET)
                return fd
            except BaseException:
                os.close(fd)
                raise
        finally:
            seccomp.seccomp_release(ctx)
    except (OSError, AttributeError) as exc:
        raise IsolationUnavailable("kernel security filter unavailable") from None


class OfflineTaskSandbox:
    """Pinned-workspace Bubblewrap launch. Never falls back to bare exec."""

    @staticmethod
    def _bwrap_path() -> str:
        path = shutil.which("bwrap", path="/usr/bin:/bin")
        if not path:
            raise IsolationUnavailable("required OS sandbox unavailable")
        details = os.stat(path, follow_symlinks=True)
        if (not stat.S_ISREG(details.st_mode)
                or details.st_mode & 0o022):
            raise IsolationUnavailable("untrusted sandbox executable")
        return path

    @staticmethod
    def clean_environment() -> dict[str, str]:
        return {"PATH": "/usr/bin:/bin", "HOME": "/workspace",
                "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                "PYTHONPATH": "", "PYTHONNOUSERSITE": "1",
                "PYTHONDONTWRITEBYTECODE": "1", "PIP_NO_INDEX": "1",
                "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
                "GIT_PAGER": "cat", "PAGER": "cat"}

    def spawn(self, root_fd: int, argv: tuple[str, ...], *, writable: bool) -> subprocess.Popen:
        if (type(root_fd) is not int or type(argv) is not tuple or not argv
                or any(type(arg) is not str or not arg or "\x00" in arg
                       for arg in argv) or type(writable) is not bool):
            raise IsolationUnavailable("invalid trusted sandbox inputs")
        if not stat.S_ISDIR(os.fstat(root_fd).st_mode):
            raise IsolationUnavailable("workspace descriptor unavailable")
        bwrap = self._bwrap_path()
        filter_fd = _network_and_escape_filter_fd()
        try:
            command = [
                bwrap, "--unshare-user", "--unshare-pid", "--unshare-ipc",
                "--unshare-uts", "--disable-userns", "--die-with-parent",
                "--clearenv",
            ]
            # Bind only system runtime directories; /usr/local and arbitrary
            # host configuration/data must not enter the sandbox root.
            for directory in ("/usr/bin", "/usr/lib", "/usr/share",
                              "/usr/libexec", "/lib", "/lib64"):
                if os.path.isdir(directory):
                    command += ["--ro-bind", directory, directory]
            command += [
                "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
                "--dir", "/etc", "--dir", "/workspace",
                "--bind-fd" if writable else "--ro-bind-fd",
                str(root_fd), "/workspace", "--chdir", "/workspace",
            ]
            for name, value in self.clean_environment().items():
                command += ["--setenv", name, value]
            command += ["--seccomp", str(filter_fd), "--", *argv]
            # stderr never contains raw worker data; fail closed on sandbox
            # setup errors, including unavailable user/mount/PID namespaces.
            return subprocess.Popen(
                command, cwd=f"/proc/self/fd/{root_fd}",
                pass_fds=(root_fd, filter_fd), stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env=self.clean_environment(), start_new_session=True,
                close_fds=True,
            )
        except OSError:
            raise IsolationUnavailable("secure task launch unavailable") from None
        finally:
            os.close(filter_fd)
