"""Build an offline tool image from explicitly selected, credential-free runtimes.

No project Dockerfile or package install hook executes on the host. Native
toolchains keep their absolute locations so managed launcher receipts remain
usable. Content, rather than an unpinned registry tag, identifies the image.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from .store import atomic_json, digest
from .types import RepairBlocked
from .storage import require_space, execution_lease


def file_digest(stream):
    result = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
        result.update(chunk)
    return result.hexdigest()


def tool_image(root, python, driver):
    # Concurrent jobs share one build context. A completed image is reusable;
    # its unpacked rootfs is always disposable, including after interrupted builds.
    root = Path(root)
    require_space(root)
    with execution_lease(root / 'toolchain'):
        fs = root / 'toolchain/rootfs'
        if fs.exists(): shutil.rmtree(fs)
        try:
            return _tool_image(root, python, driver)
        finally:
            if fs.exists(): shutil.rmtree(fs)


def _tool_image(root, python, driver):
    from .docker import run
    root = Path(root)
    probe = subprocess.check_output([str(python), '-c',
        'import json,sys,sysconfig;print(json.dumps(dict(base=sys.base_prefix,prefix=sys.prefix,'
        'stdlib=sysconfig.get_path("stdlib"),site=sysconfig.get_path("purelib"))))'], text=True)
    paths = json.loads(probe)
    stage = root / 'toolchain'
    fs = stage / 'rootfs'
    records, binaries, sources = {}, [], {}

    def copy_file(source, *, destination=None):
        source = Path(source)
        if not source.is_file(): return
        target = fs / str(destination or source).lstrip('/')
        key = str(target.relative_to(fs))
        if key in records: return
        with source.open('rb') as stream:
            elf = stream.read(4) == b'\x7fELF'
            stream.seek(0)
            content = file_digest(stream)
        records[key] = [content, source.stat().st_mode & 0o777]
        sources[key] = source
        if elf: binaries.append(source.resolve())

    def tree(source, *, exclude=()):
        source = Path(source)
        if not source.exists(): return
        for parent, directories, files in os.walk(source):
            directories[:] = [d for d in directories if d not in (*exclude, '__pycache__', '.git', '.cache')]
            for name in files:
                if name in ('.env', '.npmrc') or name.endswith('.pyc'): continue
                copy_file(Path(parent) / name)

    tree(paths['stdlib'], exclude=('site-packages',))
    tree(paths['site'])
    tree(Path(paths['prefix']) / 'bin')
    copy_file(Path(paths['prefix']) / 'pyvenv.cfg')
    tree(Path(paths['prefix']) / 'verification-tools')
    for name in ('python', 'python3', 'python3.11'):
        copy_file(Path(paths['base']) / 'bin' / name)
    for name in ('sh', 'bash', 'env', 'git', 'ls', 'cat', 'mkdir', 'rm', 'cp', 'mv', 'chmod',
                 'stat', 'readlink', 'realpath', 'touch', 'find', 'grep', 'sed', 'awk', 'head', 'tail',
                 'sort', 'uniq', 'wc', 'cut', 'tr', 'sleep', 'timeout', 'true', 'false', 'id', 'uname',
                 'which', 'tar', 'gzip', 'xargs', 'diff', 'date', 'ps', 'kill', 'unshare', 'mount',
                 'umount', 'ip', 'ss', 'curl', 'ldd'):
        source = next((Path(p) / name for p in ('/usr/bin', '/usr/sbin', '/bin', '/sbin')
                       if (Path(p) / name).is_file()), None)
        if source:
            copy_file(source)
            copy_file(source, destination='/bin/' + name)
    node = shutil.which('node')
    if node:
        copy_file(node); copy_file(node, destination='/usr/bin/node')
    codex = shutil.which('codex')
    if codex:
        resolved = Path(codex).resolve()
        tree(resolved.parents[2])  # scoped @openai npm packages, no user state
        script = '#!/bin/sh\nexec ' + str(Path(node).resolve()) + ' ' + str(resolved) + ' "$@"\n'
        records['usr/bin/codex'] = [digest(script), 0o755]
    # Include dynamic dependencies for provider binaries mounted read-only later.
    for name in ('agy', 'copilot', 'claude'):
        executable = shutil.which(name)
        if executable:
            binary = Path(executable).resolve()
            with binary.open('rb') as stream:
                if stream.read(4) == b'\x7fELF': binaries.append(binary)
    examined = set()
    while binaries:
        binary = binaries.pop()
        if binary in examined: continue
        examined.add(binary)
        result = subprocess.run(['/usr/bin/ldd', str(binary)], capture_output=True, text=True)
        for dependency in re.findall(r'(/[^\s()]+)', result.stdout):
            copy_file(dependency)
    for pattern in ('libseccomp.so*', 'libnss_*.so*', 'libresolv.so*'):
        for source in Path('/lib/x86_64-linux-gnu').glob(pattern): copy_file(source)
    for source in ('/etc/ssl/certs', '/usr/share/git-core', '/usr/lib/git-core', '/usr/share/zoneinfo'):
        tree(source)
    for source in ('/etc/nsswitch.conf', '/etc/services', '/etc/protocols', '/etc/os-release'):
        copy_file(source)
    identity = digest([records, driver, os.getuid(), os.getgid(), 1])[:24]
    image = 'auto-agents-verifier:v2-' + identity
    code, _ = run(['docker', 'image', 'inspect', image], timeout=15)
    if code:
        require_space(stage, sum(p.stat().st_size for p in sources.values()) * 2)
        for key, source in sources.items():
            target = fs / key
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            with target.open('rb') as stream:
                actual = file_digest(stream)
            if actual != records[key][0] or target.stat().st_mode & 0o777 != records[key][1]:
                raise RepairBlocked('toolchain_changed', 'toolchain changed while preparing ' + key)
        for directory in ('tmp', 'work', 'result', 'home', 'run', 'opt/repair', 'etc'):
            (fs / directory).mkdir(parents=True, exist_ok=True)
        if codex:
            (fs / 'usr/bin/codex').write_text(script)
            (fs / 'usr/bin/codex').chmod(0o755)
        (fs / 'tmp').chmod(0o1777)
        (fs / 'opt/repair/driver.py').write_text(driver)
        (fs / 'etc/passwd').write_text('root:x:0:0:root:/root:/bin/sh\n'
            f'repair:x:{os.getuid()}:{os.getgid()}:repair:/home/repair:/bin/sh\n')
        (fs / 'etc/group').write_text(f'root:x:0:\nrepair:x:{os.getgid()}:\n')
        (stage / 'Dockerfile').write_text('FROM scratch\nCOPY rootfs /\n'
            'ENV PATH=' + paths['prefix'] + '/bin:/usr/bin:/bin:/usr/sbin:/sbin\nWORKDIR /work\n')
        code, text = run(['docker', 'build', '--network=none', '-t', image, str(stage)],
                          timeout=1200, output=stage / 'build.log')
        if code: raise RepairBlocked('image_build_failed', str(stage / 'build.log') + '\n' + text[-1000:])
    atomic_json(root / 'image.json', {'image': image, 'python': str(python), 'toolchain': paths,
        'manifest': digest(records), 'driver': digest(driver), 'source': 'selected local toolchain'})
    return image
