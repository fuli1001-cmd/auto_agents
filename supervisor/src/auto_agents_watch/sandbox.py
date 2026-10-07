"""Standard, single-layer Docker boundaries. No nested metadata supervisor."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import uuid
import re
import time


def host_endpoints(value):
    return re.sub(r'(https?://)(?:localhost|127\.0\.0\.1)(?=[:/])',r'\1host.docker.internal',value)

from .process import run


class Docker:
    def __init__(self, root, *, image=None, provider=None, engine=None, base_image=None):
        self.root = Path(root)
        self.root.mkdir(parents=True,exist_ok=True)
        self.image = image or os.environ.get('AUTO_AGENTS_WATCH_IMAGE')
        self.provider = provider or {}
        self.engine = Path(engine) if engine else None
        self.base_image = os.environ.get('AUTO_AGENTS_WATCH_BASE_IMAGE') or base_image or 'node:22-bookworm-slim'
        self.deadline = None

    def time_limit(self, maximum):
        if self.deadline is None:
            return maximum
        remaining=self.deadline-time.time()
        if remaining<=0:
            raise RuntimeError('Configured maintenance duration limit reached')
        return min(maximum,remaining)

    def prepare(self):
        if not shutil.which('docker'):
            raise RuntimeError('Maintenance requires Docker; standalone business execution remains available')
        subprocess.run(['docker','info'],check=True,capture_output=True,timeout=self.time_limit(30))
        if not self.image:
            self.image = self.build()
        result = subprocess.run(['docker','image','inspect','--format','{{.Id}}',self.image],
                                check=True,capture_output=True,text=True,timeout=self.time_limit(30))
        self.image = result.stdout.strip()
        return self.image

    def build(self):
        """Prepare a tool image from the installed CLI, not a repair runtime."""
        import tempfile
        env = dict(os.environ)
        for key,value in self.provider.get('environment',{}).items():
            if value is None: env.pop(key,None)
            else: env[key] = str(value)
        kind=self.provider.get('kind','codex')
        binary = self.provider.get('binary') or {'codex':'codex','claude-code':'claude','copilot-cli':'copilot'}.get(kind,kind)
        executable = shutil.which(binary,path=env.get('PATH'))
        if not executable: raise RuntimeError('Configured maintenance CLI is unavailable: '+binary)
        actual = Path(executable).resolve()
        package = next((parent for parent in actual.parents if (parent/'package.json').is_file()),None)
        dependencies=['pytest','rich>=15,<16','regex','tomli']
        if self.engine and (self.engine/'pyproject.toml').exists():
            try: import tomllib
            except ImportError: import tomli as tomllib
            dependencies=list(dict.fromkeys(['pytest',*tomllib.loads((self.engine/'pyproject.toml').read_text())['project'].get('dependencies',[])]))
        tools=self.engine/'src/auto_agents/verification_tools' if self.engine else None
        lock=tools/'package-lock.json' if tools else None
        base_info=subprocess.run(['docker','image','inspect','--format','{{.Id}}',self.base_image],
            capture_output=True,text=True,timeout=self.time_limit(30))
        # A local image is sufficient; Docker Hub is not needed merely to
        # recheck its tag. Include its immutable identity in the tool cache.
        base_identity=base_info.stdout.strip() if base_info.returncode==0 else self.base_image
        recipe = json.dumps({'version':4,'base_image':base_identity,'dependencies':dependencies,'kind':self.provider.get('kind'),
                            'verification_tools':hashlib.sha256(lock.read_bytes()).hexdigest() if lock and lock.exists() else '',
                            'package':(package/'package.json').read_text() if package else ''},sort_keys=True)
        identity = hashlib.sha256(actual.read_bytes()+recipe.encode()).hexdigest()[:20]
        tag = 'auto-agents-watch-tools:'+identity
        exists = subprocess.run(['docker','image','inspect',tag],capture_output=True,timeout=self.time_limit(30))
        if exists.returncode == 0: return tag
        base = self.base_image
        with tempfile.TemporaryDirectory(prefix='image-',dir=self.root) as temporary:
            context = Path(temporary)
            if package:
                shutil.copytree(package,context/'cli',ignore=shutil.ignore_patterns('.env','*.log'))
                entry = '/opt/cli/'+actual.relative_to(package).as_posix()
            else:
                (context/'cli').mkdir(); shutil.copy2(actual,context/'cli'/actual.name)
                entry = '/opt/cli/'+actual.name
            (context/'requirements.txt').write_text('\n'.join(dependencies)+'\n')
            if tools and tools.is_dir():shutil.copytree(tools,context/'verification-tools',ignore=shutil.ignore_patterns('node_modules'))
            dockerfile = ('FROM '+base+'\nUSER root\n'
                + 'RUN apt-get update && apt-get install -y python3 python3-venv python3-pip git ripgrep && rm -rf /var/lib/apt/lists/*\nRUN python3 -m venv /opt/venv\nENV PATH=/opt/venv/bin:$PATH\n'
                + 'COPY cli /opt/cli\nRUN ln -sf '+entry+' /usr/local/bin/'+Path(binary).name+'\n'
                + 'COPY requirements.txt /opt/requirements.txt\nRUN python -m pip install -r /opt/requirements.txt\n'
                + ('COPY verification-tools /opt/verification-tools\nRUN npm ci --prefix /opt/verification-tools --ignore-scripts --no-audit --no-fund\nENV PATH=/opt/verification-tools/node_modules/.bin:$PATH\n' if tools and tools.is_dir() else ''))
            (context/'Dockerfile').write_text(dockerfile)
            with (self.root/'image-build.log').open('w') as output:
                try:
                    subprocess.run(['docker','build','--pull=false','-t',tag,str(context)],stdout=output,stderr=subprocess.STDOUT,check=True,timeout=self.time_limit(900))
                except (subprocess.CalledProcessError,subprocess.TimeoutExpired) as error:
                    output.flush()
                    tail=(self.root/'image-build.log').read_text(errors='replace')[-2000:]
                    raise RuntimeError('Repair tool image build failed; log: '+str(self.root/'image-build.log')+'\n'+tail) from error
        return tag

    def base(self, *, network):
        return ['docker','run','--rm','--init','--name','aa-watch-' + uuid.uuid4().hex,
            '--label','auto-agents-watch.owner='+hashlib.sha256(str(self.root.resolve()).encode()).hexdigest(),
            '--read-only','--cap-drop','ALL','--security-opt','no-new-privileges',
            '--user',str(os.getuid()) + ':' + str(os.getgid()),'--pids-limit','512',
            '--network',network,'--tmpfs','/tmp:rw,exec,nosuid,mode=1777,size=4g']

    def agent(self, argv, candidate, evidence, env, log, *, readonly=False, stdin=None, profile=None, **kwargs):
        identity=hashlib.sha256(json.dumps(self.provider,sort_keys=True).encode()).hexdigest()[:16]
        home = self.root / (('review-home-' if readonly else 'writer-home-')+identity)
        home.mkdir(exist_ok=True,mode=0o700)
        # Preserve the selected provider's native account/profile; never mount
        # the host HOME or maintenance database into the agent.
        original = Path(env.get('HOME', str(Path.home())))
        kind = self.provider.get('kind','codex')
        native = {'codex':[('.codex',env.get('CODEX_HOME'))],
                  'claude-code':[('.claude',env.get('CLAUDE_CONFIG_DIR'))],
                  'copilot-cli':[('.copilot',env.get('COPILOT_HOME'))]}.get(kind,[('.gemini',None)])
        for name,override in native:
            source = Path(override) if override else original / name
            dest = home / name
            if dest.exists():shutil.rmtree(dest)
            dest.mkdir(parents=True,exist_ok=True)
            if source.is_dir():
                for pattern in ('auth.json','config.toml','*.config.toml','settings.json','.credentials.json','config.json'):
                    for path in source.glob(pattern):
                        if path.is_file() and not path.is_symlink():
                            shutil.copy2(path,dest/path.name)
                            if 'config' in path.name or path.name=='settings.json':
                                copied=dest/path.name
                                copied.write_text(host_endpoints(copied.read_text()))
        command = self.base(network='bridge') + ['-i','--add-host','host.docker.internal:host-gateway',
            '--mount',f'type=bind,src={home.resolve()},dst=/home/agent',
            '--mount',f'type=bind,src={Path(candidate).resolve()},dst=/work' + (',readonly' if readonly else ''),
            '--mount',f'type=bind,src={Path(evidence).resolve()},dst=/evidence,readonly',
            '--workdir','/work','-e','HOME=/home/agent','-e','CODEX_HOME=/home/agent/.codex']
        metadata = Path(candidate) / '.git'
        if metadata.is_dir(): command += ['--mount',f'type=bind,src={metadata.resolve()},dst=/work/.git,readonly']
        if not readonly:
            tests = Path(candidate)/'tests'
            if tests.is_dir():
                additions=tests/'repair';additions.mkdir(exist_ok=True)
                command += ['--mount',f'type=bind,src={tests.resolve()},dst=/work/tests,readonly',
                            '--mount',f'type=bind,src={additions.resolve()},dst=/work/tests/repair']
            for name in ('supervisor','pyproject.toml','pytest.ini','conftest.py'):
                protected=Path(candidate)/name
                if protected.exists():
                    command += ['--mount',f'type=bind,src={protected.resolve()},dst=/work/{name},readonly']
        if kind=='claude-code':
            source=original/'.claude.json'
            (home/source.name).unlink(missing_ok=True)
            if source.is_file() and not source.is_symlink():shutil.copy2(source,home/source.name)
            command += ['-e','CLAUDE_CONFIG_DIR=/home/agent/.claude']
        elif kind=='copilot-cli':
            if profile:
                dest=home/'.copilot/selected-profile';dest.mkdir(parents=True,exist_ok=True)
                for name in ('config.json','settings.json','auth.json'):
                    source=Path(profile)/name
                    if source.is_file() and not source.is_symlink():
                        shutil.copy2(source,dest/name)
                        if name!='auth.json':(dest/name).write_text(host_endpoints((dest/name).read_text()))
            command += ['-e','COPILOT_HOME=/home/agent/.copilot']
        for key,value in env.items():
            if key in {'HOME','PATH','PYTHONPATH','CODEX_HOME','CLAUDE_CONFIG_DIR','COPILOT_HOME'}:continue
            prefixes={'codex':('OPENAI_','CODEX_'),'claude-code':('ANTHROPIC_','CLAUDE_'),
                      'copilot-cli':('GH_','GITHUB_','COPILOT_')}.get(kind,())
            if (key in self.provider.get('environment',{}) or key.startswith(prefixes)
                    or key.upper() in {'HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','NO_PROXY'}):
                command += ['-e',key + '=' + host_endpoints(value)]
        command += [self.image,Path(argv[0]).name,*argv[1:]]
        timeout=kwargs.pop('timeout',self.provider.get('timeout_seconds'))
        return self.execute(command,cwd=candidate,env=env,log=log,stdin=stdin,timeout=timeout,**kwargs)

    def verify(self, argv, candidate, evidence, result, log, *, project=None, extra_mounts=()):
        result = Path(result); result.mkdir(parents=True,exist_ok=True)
        command = self.base(network='none') + [
            '--security-opt','seccomp=unconfined',
            '--mount',f'type=bind,src={Path(candidate).resolve()},dst=/work,readonly',
            '--mount',f'type=bind,src={Path(evidence).resolve()},dst=/evidence,readonly',
            '--mount',f'type=bind,src={result.resolve()},dst=/result',
            '--workdir','/work','-e','HOME=/tmp/home','-e','PYTHONDONTWRITEBYTECODE=1',
            '-e','PYTHONPATH=/work/src','-e','AUTO_AGENTS_NO_SUPERVISOR=1',
            '-e','AUTO_AGENTS_OFFLINE_RESUME=1','-e','AUTO_AGENTS_STORAGE_MAINTENANCE=off']
        command += ['-e','AUTO_AGENTS_WATCH_ISOLATED=1']
        if self.engine:
            command += ['-e','AUTO_AGENTS_ENGINE_SOURCE_ROOT='+str(self.engine.resolve())]
        if project:
            project_root=argv[argv.index('--project')+1]
            command += ['--mount',f'type=bind,src={Path(project).resolve()},dst={project_root}']
            manifest=Path(project)/'.auto-agents/state/offline-checkouts.json'
            if manifest.is_file():
                for destination,relative in json.loads(manifest.read_text()).items():
                    private=(Path(project)/relative).resolve()
                    target=Path(destination)
                    if (not private.is_relative_to(Path(project).resolve()) or not target.is_absolute()
                            or target.is_relative_to(Path(project_root)) or not (private/'.git').is_dir()):
                        raise ValueError('Invalid native checkout snapshot binding')
                    # A copied tree has a new inode. Rebind only the private
                    # registration; goal, source descriptors and receipts stay intact.
                    custody=Path(project)/'.auto-agents/state/custody'/(
                        hashlib.sha256(json.dumps(destination,sort_keys=True,ensure_ascii=False).encode()).hexdigest()+'.json')
                    if not custody.is_file():raise ValueError('Native checkout registration is unavailable')
                    record=json.loads(custody.read_text())
                    record.update(device=private.stat().st_dev,inode=private.stat().st_ino)
                    custody.write_text(json.dumps(record))
                    command+=['--mount',f'type=bind,src={private},dst={destination}']
            # Dependencies are immutable inputs, separate from the writable
            # snapshot. Tool caches stay private to this container.
            for relative in ('.conda','.venv','node_modules','workbench/node_modules'):
                dependency=Path(project_root)/relative
                if dependency.is_dir():
                    destination=Path(project_root)/relative
                    command += ['--mount',f'type=bind,src={dependency.resolve()},dst={destination},readonly']
                    if destination.name=='node_modules':
                        command += ['--tmpfs',str(destination/'.vite')+':rw,nosuid,mode=1777']
        for source,destination in extra_mounts:
            command += ['--mount',f'type=bind,src={Path(source).resolve()},dst={destination},readonly']
        command += [self.image,*argv]
        return self.execute(command,cwd=candidate,env=os.environ,log=log,timeout=7200)

    def execute(self, command, **kwargs):
        name=command[command.index('--name')+1]
        callback=kwargs.pop('on_start',None)
        if self.deadline:
            remaining=self.deadline-time.time()
            if remaining<=0:raise RuntimeError('Configured maintenance duration limit reached')
            timeout=kwargs.get('timeout')
            kwargs['timeout']=min(timeout,remaining) if timeout else remaining
        def started(record):
            if callback: callback({**record,'container':name,'container_owner':hashlib.sha256(str(self.root.resolve()).encode()).hexdigest()})
        try:
            return run(command,on_start=started,**kwargs)
        finally:
            # Stop only this exact owned container, even if its client died.
            subprocess.run(['docker','rm','-f',name],capture_output=True,timeout=15)
