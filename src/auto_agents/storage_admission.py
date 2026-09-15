"""Prevent large copies from exhausting Linux or WSL backing storage."""
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

RESERVE_BYTES = 2 * 1024 ** 3


class DiskSpaceError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


@lru_cache(maxsize=1)
def windows_backing_roots():
    """WSL's virtual filesystem free space does not measure its host drive."""
    distro = os.environ.get('WSL_DISTRO_NAME', '')
    if not distro:
        return ()
    executable = Path('/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe')
    command = (r'Get-ChildItem HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss | '
               'ForEach-Object { Get-ItemProperty $_.PSPath | '
               'Select-Object DistributionName,BasePath } | ConvertTo-Json -Compress')
    try:
        result = subprocess.run([str(executable), '-NoProfile', '-NonInteractive', '-Command', command],
                                capture_output=True, text=True, timeout=10, check=True)
        rows = json.loads(result.stdout)
        if isinstance(rows, dict): rows = [rows]
        paths = [row['BasePath'] for row in rows if row['DistributionName'] in
                 (distro, 'docker-desktop-data', 'docker-desktop')]
        if not any(row['DistributionName'] == distro for row in rows):
            raise ValueError('current distribution is absent from the WSL registry')
        roots = set()
        for path in paths:
            match = re.search(r'([A-Za-z]):\\', path)
            if not match:
                raise ValueError('unsupported WSL backing path')
            root = Path('/mnt') / match[1].lower()
            if not root.is_mount():
                raise ValueError('Windows backing drive is not mounted: ' + str(root))
            roots.add(root)
        return tuple(sorted(roots))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        raise DiskSpaceError('disk_observation',
            'Could not measure WSL backing storage; Linux free bytes alone are insufficient: ' + str(error)) from error


def require_space(root, additional=0):
    path = Path(root).absolute()
    while not path.exists():
        path = path.parent
    needed = RESERVE_BYTES + additional
    for filesystem in (path, *windows_backing_roots()):
        free = shutil.disk_usage(filesystem).free
        if free < needed:
            raise DiskSpaceError('disk_space',
                f'Insufficient disk space at {filesystem}: {free} bytes free; '
                f'{needed} required including a {RESERVE_BYTES}-byte recovery reserve. '
                'Candidate and evidence are preserved; free space before resuming.')


class CopyAdmission:
    """Check before starting, then per 64 MiB or second during tree copies."""
    def __init__(self, destination):
        self.destination = Path(destination)
        self.bytes = 0
        self.next_check = 0.0
        require_space(self.destination, 64 * 1024 ** 2)

    def __call__(self, source, target, *, follow_symlinks=True):
        size = os.stat(source, follow_symlinks=follow_symlinks).st_size
        self.bytes += size
        now = time.monotonic()
        if self.bytes >= 64 * 1024 ** 2 or now >= self.next_check:
            require_space(self.destination, max(size, 64 * 1024 ** 2))
            self.bytes = 0
            self.next_check = now + 1
        return shutil.copy2(source, target, follow_symlinks=follow_symlinks)
