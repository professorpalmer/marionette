"""The optional observation producer must not require candidate PM imports at boot."""
import os
from pathlib import Path
import subprocess
import sys


def test_native_producer_boots_without_metadata_reader_or_new_pm_contracts(tmp_path):
    code = '''
import builtins
import threading
from types import SimpleNamespace
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name in ('puppetmaster.identity', 'puppetmaster.contracts', 'harness.job_readmodel'):
        raise ModuleNotFoundError(name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from harness.local_jobs import LocalJobsMixin
class Runner(LocalJobsMixin):
    pass
runner = Runner()
runner._local_jobs = {}
runner._local_jobs_lock = threading.Lock()
runner._local_jobs_path = PATH
runner._load_local_jobs()
assert runner.local_metadata_handle().describe()['available']
assert runner.local_metadata_handle().rows == {}
'''.replace('PATH', repr(str(tmp_path / 'jobs.json')))
    result = subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).resolve().parents[1],
                            env=os.environ.copy(), capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
