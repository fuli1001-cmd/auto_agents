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
import shlex
import shutil
import subprocess
import sys
import io
import tarfile
import threading
import uuid

from .store import atomic_json, digest
from .types import RepairBlocked
from .storage import require_space, execution_lease


def file_digest(stream):
    result = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
        result.update(chunk)
    return result.hexdigest()


def tool_image(root, python, driver, *, codex_binary=None):
    # Concurrent jobs share one build context. A completed image is reusable;
    # its unpacked rootfs is always disposable, including after interrupted builds.
    root = Path(root)
    require_space(root)
    with execution_lease(root / 'toolchain'):
        fs = root / 'toolchain/rootfs'
        if fs.exists(): shutil.rmtree(fs)
        try:
            return _tool_image(root, python, driver, codex_binary=codex_binary)
        finally:
            if fs.exists(): shutil.rmtree(fs)


def _tool_image(root, python, driver, *, codex_binary=None):
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
        source = Path(os.path.normpath(str(source)))
        if not source.is_file(): return
        target = fs / os.path.normpath(str(destination or source)).lstrip('/')
        key = str(target.relative_to(fs))
        if key in records: return
        if source.is_symlink() and destination is None:
            records[key] = ['link', os.readlink(source)]
            sources[key] = source
            linked = Path(os.readlink(source))
            copy_file(linked if linked.is_absolute() else source.parent / linked)
            return
        with source.open('rb') as stream:
            elf = stream.read(4) == b'\x7fELF'
            stream.seek(0)
            content = file_digest(stream)
        records[key] = [content, source.stat().st_mode & 0o777]
        sources[key] = source
        if elf: binaries.append(source.resolve())

    def tree(source, *, exclude=(), destination=None):
        source = Path(source)
        if not source.exists(): return
        for parent, directories, files in os.walk(source):
            directories[:] = [d for d in directories if d not in (*exclude, '__pycache__', '.git', '.cache')]
            for name in files:
                if name in ('.env', '.npmrc') or name.endswith('.pyc'): continue
                target = Path(destination) / Path(parent).relative_to(source) / name if destination is not None else None
                copy_file(Path(parent) / name, destination=target)

    tree(paths['stdlib'], exclude=('site-packages',))
    tree(paths['site'])
    base_site = Path(paths['stdlib']) / 'site-packages'
    if base_site != Path(paths['site']):
        tree(paths['site'], destination=base_site)
    tree(Path(paths['prefix']) / 'bin')
    copy_file(Path(paths['prefix']) / 'pyvenv.cfg')
    tree(Path(paths['prefix']) / 'verification-tools')
    for name in ('python', 'python3', 'python3.11'):
        copy_file(Path(paths['base']) / 'bin' / name)
    for name in ('sh', 'bash', 'env', 'git', 'ls', 'cat', 'mkdir', 'rm', 'cp', 'mv', 'chmod',
                 'stat', 'readlink', 'realpath', 'touch', 'find', 'grep', 'sed', 'awk', 'head', 'tail',
                 'sort', 'uniq', 'wc', 'cut', 'tr', 'sleep', 'timeout', 'true', 'false', 'id', 'uname',
                 'which', 'tar', 'gzip', 'xargs', 'diff', 'date', 'ps', 'kill', 'unshare', 'mount',
                 'umount', 'ip', 'ss', 'curl', 'ldd', 'openssl', 'dirname', 'basename', 'printf',
                 'du', 'df', 'mktemp', 'tee', 'ln', 'rmdir', 'expr', 'rg'):
        source = next((Path(p) / name for p in ('/usr/bin', '/usr/sbin', '/bin', '/sbin')
                       if (Path(p) / name).is_file()), None)
        if source is None and shutil.which(name): source = Path(shutil.which(name))
        if source:
            copy_file(source)
            copy_file(source, destination='/bin/' + name)
            copy_file(source, destination='/usr/bin/' + name)
    node = shutil.which('node')
    if node:
        copy_file(node); copy_file(node, destination='/usr/bin/node')
    npm = shutil.which('npm')
    if npm and node:
        npm_script = Path(npm).resolve()
        npm_root = npm_script.parent.parent
        if not (npm_root / 'package.json').is_file():
            raise RepairBlocked('toolchain_unavailable', 'cannot identify the npm installation')
        tree(npm_root)
        for name, entry in (('npm', npm_script), ('npx', npm_root / 'bin/npx-cli.js')):
            wrapper = '#!/bin/sh\nexec ' + shlex.quote(str(Path(node).resolve())) + ' ' + shlex.quote(str(entry)) + ' "$@"\n'
            # Generated wrappers are written only when the image needs building.
            records['usr/bin/' + name] = [digest(wrapper), 0o755]
            sources['@wrapper:' + name] = wrapper
    codex = shutil.which(codex_binary or 'codex')
    script = None
    if codex:
        resolved = Path(codex).resolve()
        with resolved.open('rb') as stream: native = stream.read(4) == b'\x7fELF'
        if native:
            copy_file(resolved, destination='/usr/bin/codex')
        elif 'node_modules' in resolved.parts and '@openai' in resolved.parts and node:
            scope = Path(*resolved.parts[:resolved.parts.index('@openai') + 1])
            tree(scope)
            script = '#!/bin/sh\nexec ' + shlex.quote(str(Path(node).resolve())) + ' ' + shlex.quote(str(resolved)) + ' "$@"\n'
            records['usr/bin/codex'] = [digest(script), 0o755]
        else:
            raise RepairBlocked('provider_configuration', 'Codex image requires an official Node installation or standalone ELF executable')
    conda = shutil.which('conda')
    if conda:
        executable = Path(conda).resolve()
        prefix = next((p for p in executable.parents if (p / 'conda-meta').is_dir()), None)
        if prefix is None:
            raise RepairBlocked('toolchain_unavailable', 'cannot identify the configured Conda distribution')
        packages = {}
        for record in (prefix / 'conda-meta').glob('*.json'):
            value = json.loads(record.read_text())
            packages[value['name']] = value
        needed, visited = ['conda'], set()
        while needed:
            name = needed.pop()
            if name in visited or name.startswith('__'): continue
            visited.add(name)
            record = packages.get(name)
            if record is None:
                raise RepairBlocked('toolchain_unavailable', 'Conda dependency is missing: ' + name)
            needed.extend(re.split(r'[ <>=!]', dependency, maxsplit=1)[0] for dependency in record.get('depends', []))
            for relative in record.get('files', []):
                path = Path(relative)
                if path.is_absolute() or '..' in path.parts:
                    raise RepairBlocked('toolchain_path', 'unsafe Conda package manifest path')
                copy_file(prefix / path)
        for alias in ('python', 'python3', 'conda'):
            copy_file(prefix / 'bin' / alias)
        copy_file(executable, destination='/usr/bin/conda')
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
    for source in ('/etc/nsswitch.conf', '/etc/services', '/etc/protocols', '/etc/os-release', '/usr/lib/ssl/openssl.cnf'):
        copy_file(source)
    from . import images
    identity = digest([records, os.getuid(), os.getgid(), images.owner(), 3])[:24]
    image = 'auto-agents-verifier:v2-' + identity
    code, _ = run(['docker', 'image', 'inspect', image], timeout=15)
    if code:
        generated = {
            'opt/repair/driver.py': (driver, 0o644),
            'etc/passwd': ('root:x:0:0:root:/root:/bin/sh\n'
                f'repair:x:{os.getuid()}:{os.getgid()}:repair:/home/repair:/bin/sh\n', 0o644),
            'etc/group': (f'root:x:0:\nrepair:x:{os.getgid()}:\n', 0o644),
        }
        if script is not None: generated['usr/bin/codex'] = (script, 0o755)
        for key, value in sources.items():
            if key.startswith('@wrapper:'): generated['usr/bin/' + key.split(':', 1)[1]] = (value, 0o755)
        total = sum(p.stat().st_size for key, p in sources.items()
                    if not key.startswith('@wrapper:') and records[key][0] != 'link')
        require_space(stage, total)
        import_image(image, records, sources, generated, paths['prefix'], stage / 'build.log')
    code, text = run(['docker', 'run', '--rm', '--network', 'none', '--read-only',
        '--user', f'{os.getuid()}:{os.getgid()}', '-e', 'PYTHONDONTWRITEBYTECODE=1', image, 'python', '-c',
        'import sqlite3,ssl,bz2,lzma,ctypes,uuid,regex,pytest; print("toolchain ready")'], timeout=30)
    if code: raise RepairBlocked('image_validation_failed', text[-2000:])
    # Native agents use these utilities for repository discovery. A missing
    # dirname previously turned a parent walk into an endless shell loop.
    code, text = run(['docker', 'run', '--rm', '--network', 'none', '--read-only', image,
        '/bin/sh', '-ec', 'test "$(dirname /work/tests)" = /work; '
        'test "$(basename /work/tests)" = tests; command -v mktemp >/dev/null; rg --version'], timeout=15)
    if code: raise RepairBlocked('image_validation_failed', text[-2000:])
    code, text = run(['docker', 'run', '--rm', '--network', 'none', '--read-only', image,
        str(Path(paths['base']) / 'bin/python'), '-c', 'import pytest,regex; print("base interpreter ready")'], timeout=30)
    if code: raise RepairBlocked('image_validation_failed', text[-2000:])
    if conda:
        code, text = run(['docker', 'run', '--rm', '--network', 'none', '--read-only', image, 'conda', '--version'], timeout=30)
        if code: raise RepairBlocked('image_validation_failed', text[-2000:])
    code, image_id = run(['docker', 'image', 'inspect', image, '--format', '{{.Id}}'], timeout=15)
    if code: raise RepairBlocked('image_unavailable', image_id)
    images.record(image_id.strip(), image)
    atomic_json(root / 'image.json', {'image': image, 'python': str(python), 'toolchain': paths,
        'manifest': digest(records), 'driver': digest(driver), 'source': 'selected local toolchain'})
    return image


def import_image(image, records, sources, generated, prefix, logfile):
    """Stream only verified tool files into Docker; never duplicate the rootfs."""
    from .docker import run
    from . import images
    temporary = 'auto-agents-verifier:building-' + uuid.uuid4().hex
    command = ['docker', 'import', '--change', 'ENV PATH=' + prefix + '/bin:/usr/bin:/bin:/usr/sbin:/sbin',
               '--change', 'WORKDIR /work', '--change', 'LABEL org.auto-agents.purpose=repair-verifier-v2',
               '--change', 'LABEL org.auto-agents.registry=' + images.owner(), '-', temporary]
    class CheckedReader:
        def __init__(self, stream): self.stream, self.digest = stream, hashlib.sha256()
        def read(self, size=-1):
            data = self.stream.read(size); self.digest.update(data); return data
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)
    def expire():
        if process.poll() is None: process.kill()
    watchdog = threading.Timer(1200, expire); watchdog.daemon = True; watchdog.start()
    try:
        with tarfile.open(fileobj=process.stdin, mode='w|') as archive:
            for name in ('tmp', 'work', 'result', 'home', 'run', 'opt', 'opt/repair', 'etc', 'usr', 'usr/bin'):
                info = tarfile.TarInfo(name); info.type = tarfile.DIRTYPE
                info.mode = 0o1777 if name == 'tmp' else 0o755
                archive.addfile(info)
            for key, source in sorted(sources.items()):
                if key.startswith('@wrapper:') or key in generated: continue
                info = tarfile.TarInfo(key)
                expected = records[key]
                if expected[0] == 'link':
                    if not source.is_symlink() or os.readlink(source) != expected[1]:
                        raise RepairBlocked('toolchain_changed', 'tool link changed during capture: ' + key)
                    info.type, info.linkname, info.mode = tarfile.SYMTYPE, expected[1], 0o777
                    archive.addfile(info)
                else:
                    info.size, info.mode = source.stat().st_size, expected[1]
                    with source.open('rb') as stream:
                        checked = CheckedReader(stream)
                        archive.addfile(info, checked)
                        if checked.digest.hexdigest() != expected[0] or source.stat().st_mode & 0o777 != expected[1]:
                            raise RepairBlocked('toolchain_changed', 'tool bytes changed during capture: ' + key)
            for key, (text, mode) in generated.items():
                data = text.encode(); info = tarfile.TarInfo(key); info.size = len(data); info.mode = mode
                archive.addfile(info, io.BytesIO(data))
        process.stdin.close(); process.stdin = None
        stdout, stderr = process.communicate(timeout=1200)
        logfile.write_bytes((stdout + stderr)[-2 * 1024 ** 2:])
        if process.returncode:
            raise RepairBlocked('image_build_failed', stderr.decode(errors='replace')[-2000:])
        code, text = run(['docker', 'tag', temporary, image], timeout=30)
        if code: raise RepairBlocked('image_build_failed', text[-2000:])
    finally:
        watchdog.cancel()
        if process.poll() is None:
            process.kill(); process.wait(timeout=10)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None: stream.close()
        run(['docker', 'image', 'rm', temporary], timeout=30)
