"""pip install that waits out PyPI index lag for a just-published release.

A Marionette pin bump lands minutes after `twine upload`; pip's simple index
and CDN can still lack the new puppetmaster-ai version and fail with "No
matching distribution found". Only that error is retried (cache bypassed,
with backoff); every other pip failure fails at once.
"""
from __future__ import annotations

import subprocess
import sys
import time

_LAGGING = "No matching distribution found for puppetmaster-ai"
_WAITS = (30, 60, 90, 120, 180)


def main(argv, run=subprocess.run, sleep=time.sleep):
    for attempt in range(len(_WAITS) + 1):
        command = [sys.executable, "-m", "pip", "install", *argv]
        if attempt:
            command.append("--no-cache-dir")
        result = run(command, capture_output=True, text=True)
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        if result.returncode == 0:
            return 0
        if _LAGGING not in result.stderr or attempt == len(_WAITS):
            return result.returncode
        print(f"puppetmaster-ai not on the index yet; retrying in {_WAITS[attempt]}s", flush=True)
        sleep(_WAITS[attempt])
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
