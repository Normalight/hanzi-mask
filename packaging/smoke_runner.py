"""Run a frozen smoke-test in a sanitized environment with one retry.

Used by the Windows native-build workflow: the build step environment carries
MSVC, installer and updater variables that can perturb DLL search or trigger
first-run scans; the smoke must run with a minimal, known-good environment
from a neutral working directory, matching the plain build pipeline.
"""
import os
import subprocess
import sys
import tempfile

KEEP = {'SYSTEMROOT', 'SYSTEMDRIVE', 'COMSPEC', 'TEMP', 'TMP',
        'USERPROFILE', 'LOCALAPPDATA', 'APPDATA', 'HOMEDRIVE', 'HOMEPATH'}


def main():
    if len(sys.argv) != 2:
        raise SystemExit('usage: smoke_runner.py <frozen-executable>')
    exe = sys.argv[1]
    env = {key: value for key, value in os.environ.items() if key in KEEP}
    env['PATH'] = r'C:\Windows\System32;C:\Windows;C:\Windows\System32\Wbem'
    env['QT_QPA_PLATFORM'] = 'offscreen'
    env['PYTHONUTF8'] = '1'
    for attempt in (1, 2):
        try:
            subprocess.run([exe, '--smoke-test'], cwd=tempfile.gettempdir(),
                           env=env, check=True, timeout=240)
            return
        except subprocess.TimeoutExpired:
            print(f'smoke attempt {attempt} timed out; {"retrying" if attempt == 1 else "giving up"}',
                  flush=True)
    raise SystemExit('smoke-test timed out twice in a sanitized environment')


if __name__ == '__main__':
    main()
