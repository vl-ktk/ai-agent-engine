"""Private process start gate for crash-safe task attachment.

The worker must not run before its process identity is durably registered.
If its parent crashes before releasing this pipe, EOF cancels execution.
Only a trusted controller constructs this argv; no HTTP/model endpoint.
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    if len(sys.argv) < 3:
        return 124
    try:
        fd = int(sys.argv[1])
        if fd <= 2:
            return 124
        ready = os.read(fd, 1)
        os.close(fd)
        if ready != b"G":
            return 125
        argv = sys.argv[2:]
        os.execvpe(argv[0], argv, os.environ)
    except (OSError, ValueError):
        return 126
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
