"""Run installed pytest with sterile settings and fail-closed runtime guards.

Usage: .venv/bin/python -B scripts/run_isolated_tests.py tests/ -q --tb=short
This does not start polling or load live environment settings. Runtime data,
secrets, recovery snapshots and writable checkpoint paths remain blocked. One
historical benchmark may read its own source-only baseline file (see below);
that exception is exact-path and read-only.
"""
import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRATCH = Path('/tmp/opencode/study_bot_audit_tests')
SCRATCH.mkdir(parents=True, exist_ok=True, mode=0o700)
RUN = Path(tempfile.mkdtemp(prefix='architecture_phase2_', dir=SCRATCH))
RUN.chmod(0o700)
os.environ.clear()
os.environ.update(PATH='/usr/bin:/bin', HOME=str(RUN),
                  PYTHONDONTWRITEBYTECODE='1', PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
sys.dont_write_bytecode = True
# Keep importlib away from any pre-existing __pycache__ in external source
# checkpoints; the benchmark must load the reviewed .py source, not a .pyc.
sys.pycache_prefix = str(RUN / 'pycache')
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))


# A pre-existing benchmark compares current capture code with a source-only
# snapshot from an earlier phase. Only this exact file is readable; it contains
# no runtime data and no secret. Every write and every other checkpoint path
# remains blocked.
READ_ONLY_SOURCE_BASELINE = Path(
    '/home/igor/coliseum_build_checkpoints/phase2_prebuild/services/chat_capture.py'
).resolve()


def check_path(value, *, write=False, mode=None):
    if not isinstance(value, (str, bytes, os.PathLike)):
        return
    path = Path(os.fsdecode(value)).absolute()
    if (
        not write
        and path.resolve() == READ_ONLY_SOURCE_BASELINE
        and mode in (None, 'r', 'rb', 'rt')
    ):
        return
    forbidden = [ROOT/'storage', ROOT/'imgs', ROOT/'backups',
                 Path('/home/igor/study_bot_audit'), Path('/mnt/d/tgbot'),
                 Path('/home/igor/coliseum_build_checkpoints')]
    if any(path == directory or path.is_relative_to(directory) for directory in forbidden):
        raise RuntimeError('Safety wrapper: protected runtime/checkpoint access denied')
    if any(part.startswith(('.env', '.recovery_snapshot')) for part in path.parts):
        raise RuntimeError('Safety wrapper: secret/recovery access denied')


def audit(event, args):
    if event == 'sqlite3.connect':
        path = Path(str(args[0])).resolve()
        if not path.is_relative_to(SCRATCH.resolve()):
            raise RuntimeError('Safety wrapper: SQLite outside synthetic scratch denied')
    elif event in {'socket.connect', 'socket.getaddrinfo', 'socket.gethostbyname'}:
        raise RuntimeError('Safety wrapper: network denied')
    elif event in {'subprocess.Popen', 'os.system', 'os.exec', 'os.posix_spawn'}:
        raise RuntimeError('Safety wrapper: child process execution denied')
    elif event == 'open':
        mode = args[1] if len(args) > 1 else None
        write = isinstance(mode, str) and any(flag in mode for flag in 'wax+')
        check_path(args[0], write=write, mode=mode)
    elif event in {'os.scandir', 'os.remove', 'os.rmdir', 'os.mkdir'}:
        check_path(args[0], write=event != 'os.scandir')
    elif event in {'os.rename', 'os.link', 'os.symlink'}:
        check_path(args[0], write=True)
        check_path(args[1], write=True)


sys.addaudithook(audit)
import pytest  # noqa: E402 -- install safety guards BEFORE plugin/collection imports

print('SAFETY: sterile env, audit guards before collection, unique basetemp:', RUN, flush=True)
result = pytest.main(['-p', 'pytest_asyncio.plugin', '-p', 'pytest_timeout',
                      '-p', 'no:cacheprovider', '-o', 'addopts=',
                      '--basetemp='+str(RUN/'pytest'), *sys.argv[1:]])
print('PYTEST_EXIT_CODE:', int(result), flush=True)
raise SystemExit(result)
