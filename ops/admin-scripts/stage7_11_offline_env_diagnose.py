"""One-shot Netcup 7.11 isolated OFFLINE traceback metadata only.

No OAuth access, model requests, account status queries, production DB
mutation or service changes. Trusted Python in private temporary source
prints only exception class, source-code function and line number.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import stat
import tempfile


_PATH = Path("ops/admin-scripts/stage7_11_capacity_live_acceptance.py")
_CODE = r"""
import json,sys
report = {'status':'OFFLINE_BLOCKED','exception':'unknown',
          'function':'unknown','line':0,'source':'unknown',
          'missing_module':'none','private_package_visible':False,
          'source_file_readable':False}
try:
    import importlib.util,os
    report['source_file_readable'] = (os.path.isfile(
        'ai_agent_engine/task_stage7_11_acceptance.py') and os.access(
        'ai_agent_engine/task_stage7_11_acceptance.py',os.R_OK))
    spec = importlib.util.find_spec('ai_agent_engine')
    report['private_package_visible'] = bool(spec and spec.origin and
        os.path.realpath(spec.origin) == os.path.realpath('ai_agent_engine/__init__.py'))
    from ai_agent_engine.task_stage7_11_acceptance import run_acceptance
    out = run_acceptance(live=False)
    if out.get('status') != 'OFFLINE_PASS':
        raise RuntimeError('offline_receipt_unexpected')
    report.update(status='OFFLINE_PASS',exception='none',
                  function='none',line=0,source='none')
except BaseException as exc:
    if isinstance(exc, ModuleNotFoundError):
        name = getattr(exc,'name',None)
        if (isinstance(name,str) and len(name) <= 80 and
                1 <= len(name.split('.')) <= 6 and
                all(part.isidentifier() and part.isascii()
                    for part in name.split('.'))):
            report['missing_module'] = name
        else:
            report['missing_module'] = 'unknown'
    stack = []
    tb = exc.__traceback__
    while tb is not None:
        frame = tb.tb_frame
        filename = frame.f_code.co_filename
        if '/ai_agent_engine/' in filename or filename.endswith('task_stage7_11_acceptance.py'):
            stack.append((frame.f_code.co_name,tb.tb_lineno,filename.rsplit('/',1)[-1]))
        tb=tb.tb_next
    name,number,source = stack[-1] if stack else ('unknown',0,'unknown')
    report.update(status='OFFLINE_BLOCKED',exception=type(exc).__name__,
                  function=name,line=number,source=source)
print('STAGE7_11_OFFLINE_METADATA '+json.dumps(report,sort_keys=True),flush=True)
sys.exit(0 if report['status']=='OFFLINE_PASS' else 2)
"""


def _load_reviewed_helper():
    if _PATH.is_symlink() or not _PATH.is_file():
        raise RuntimeError("reviewed owner helper unavailable")
    spec = importlib.util.spec_from_file_location("owner_711_preflight", _PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("reviewed helper cannot load")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _parse_metadata(output: bytes) -> dict:
    for line in output.splitlines():
        if not line.startswith(b"STAGE7_11_OFFLINE_METADATA "):
            continue
        try:
            result = json.loads(line.split(b" ", 1)[1])
        except (UnicodeError, ValueError, TypeError):
            raise RuntimeError("offline metadata corrupt") from None
        if (type(result) is not dict
                or set(result) != {"status", "exception", "function", "line", "source",
                                   "missing_module", "private_package_visible",
                                   "source_file_readable"}
                or result["status"] not in ("OFFLINE_PASS", "OFFLINE_BLOCKED")
                or any(type(result[name]) is not str
                       or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", result[name])
                       for name in ("exception", "function"))
                or type(result["source"]) is not str
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,79}", result["source"])
                or type(result["missing_module"]) is not str
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){0,5}",
                                    result["missing_module"])
                or type(result["private_package_visible"]) is not bool
                or type(result["source_file_readable"]) is not bool
                or type(result["line"]) is not int
                or not 0 <= result["line"] <= 10000):
            raise RuntimeError("offline metadata untrusted")
        return result
    return {"status": "OFFLINE_BLOCKED", "exception": "ProcessUnavailable",
            "function": "unknown", "source": "unknown", "line": 0,
            "missing_module": "unknown", "private_package_visible": False,
            "source_file_readable": False}


def main() -> None:
    if (os.geteuid() != 0 or not all(os.environ.get(name) for name in
        ("AE_EXPECTED_CURRENT", "AE_SOURCE_SHA", "AE_REQUEST_SHA",
         "AE_REQUEST_ID", "AE_EVIDENCE"))):
        raise RuntimeError("owner authentication required")
    helper = _load_reviewed_helper()
    if os.environ["AE_EXPECTED_CURRENT"] != helper.EXPECTED_RELEASE:
        raise RuntimeError("unexpected release target")
    runtime = pwd.getpwnam("ae-runtime")
    before = helper._baseline(runtime.pw_uid)
    evidence = Path(os.environ["AE_EVIDENCE"])
    meta = evidence.lstat()
    if (not stat.S_ISDIR(meta.st_mode) or meta.st_uid != 0
            or stat.S_IMODE(meta.st_mode) != 0o700):
        raise RuntimeError("private owner evidence unavailable")
    with tempfile.TemporaryDirectory(prefix="ae-711-offline-probe-", dir="/var/tmp") as tmp:
        directory = Path(tmp)
        os.chown(directory, runtime.pw_uid, runtime.pw_gid)
        source = helper._prepare_disposable_source(
            directory, owner_uid=runtime.pw_uid, owner_gid=runtime.pw_gid)
        # Independent POSIX access check: works even when Python exits before
        # it can print the offline receipt. No file contents leave Netcup.
        runtime_file_readable = helper._owner_command(runtime,
            ["/usr/bin/test", "-r", str(source / "ai_agent_engine" /
                                    "task_stage7_11_acceptance.py")],
            source=source, timeout=8)[0]
        runtime_pkg_traversable = helper._owner_command(runtime,
            ["/usr/bin/test", "-x", str(source / "ai_agent_engine")],
            source=source, timeout=8)[0]
        ran, stdout = helper._owner_command(
            runtime, [str(helper.PYTHON), "-c", _CODE],
            source=source, timeout=80)
        result = _parse_metadata(stdout)
        if ran != (result["status"] == "OFFLINE_PASS"):
            result["status"] = "OFFLINE_BLOCKED"
            result["exception"] = "RunnerExitMismatch"
        if helper._baseline(runtime.pw_uid) != before:
            raise RuntimeError("live state changed during offline diagnostic")
        report = {"schema": 1, "scope": "vl-ktk/ai-agent-engine",
                  "live_release": helper.EXPECTED_RELEASE,
                  "offline": result, "model_calls": False,
                  "runtime_file_readable": runtime_file_readable,
                  "runtime_package_traversable": runtime_pkg_traversable,
                  "oauth_refresh": False, "working_sqlite_modified": False,
                  "services_modified": False, "credentials_printed": False}
        with (evidence / "stage7-11-offline-diagnose.json").open("x", encoding="utf-8") as f:
            json.dump(report, f, sort_keys=True, separators=(",", ":"))
            f.write("\n")
        print("STAGE7_11_OFFLINE_NETCUP_DIAGNOSTIC " +
              json.dumps(report, sort_keys=True, separators=(",", ":")), flush=True)
        # A correctly observed failure is a successful diagnosis, not a
        # misrepresentation of completed stage 7.11 acceptance.


if __name__ == "__main__":
    main()
