"""Run a frozen smoke-test with diagnostics: inherited env first, sanitized retry.

Used by the Windows native-build workflow to isolate why the frozen smoke can
stall before producing output: each attempt captures the child's stdout/stderr
and prints it whether the run succeeds, fails or times out.
"""
import os
import subprocess
import sys
import tempfile

KEEP = {'SYSTEMROOT', 'SYSTEMDRIVE', 'COMSPEC', 'TEMP', 'TMP',
        'USERPROFILE', 'LOCALAPPDATA', 'APPDATA', 'HOMEDRIVE', 'HOMEPATH'}


def sanitized_env():
    env = {key: value for key, value in os.environ.items() if key in KEEP}
    env['PATH'] = r'C:\Windows\System32;C:\Windows;C:\Windows\System32\Wbem'
    env['QT_QPA_PLATFORM'] = 'offscreen'
    env['PYTHONUTF8'] = '1'
    return env


def attempt(exe, label, env, cwd):
    try:
        result = subprocess.run([exe, '--smoke-test'], cwd=cwd, env=env,
                                capture_output=True, text=True, timeout=240)
        print(f'[{label}] exit={result.returncode}', flush=True)
        print(result.stdout, end='', flush=True)
        print(result.stderr, end='', file=sys.stderr, flush=True)
        return result.returncode == 0
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode(errors='replace') if isinstance(exc.stdout, bytes) else (exc.stdout or '')
        err = exc.stderr.decode(errors='replace') if isinstance(exc.stderr, bytes) else (exc.stderr or '')
        print(f'[{label}] timed out; captured output:', flush=True)
        print(out, end='', flush=True)
        print(err, end='', file=sys.stderr, flush=True)
        return False


def main():
    if len(sys.argv) != 2:
        raise SystemExit('usage: smoke_runner.py <frozen-executable>')
    exe = sys.argv[1]
    if attempt(exe, 'inherited-env', None, tempfile.gettempdir()):
        return
    if attempt(exe, 'sanitized-env', sanitized_env(), tempfile.gettempdir()):
        return
    raise SystemExit('smoke-test failed in both inherited and sanitized environments')


if __name__ == '__main__':
    main()
