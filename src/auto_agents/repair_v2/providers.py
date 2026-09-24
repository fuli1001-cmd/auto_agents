"""Native planning/implementation protocols behind one phase interface."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from urllib.parse import urlsplit, urlunsplit

from .types import AgentReply, RepairBlocked


def bridge_url(value):
    parsed = urlsplit(value)
    if parsed.hostname not in ('127.0.0.1', 'localhost', '::1'): return value
    userinfo = parsed.netloc.rsplit('@', 1)[0] + '@' if '@' in parsed.netloc else ''
    authority = userinfo + 'host.docker.internal' + (':' + str(parsed.port) if parsed.port else '')
    return urlunsplit((parsed.scheme, authority, parsed.path, parsed.query, parsed.fragment))


def toml(value):
    if isinstance(value, dict):
        return '{' + ', '.join(json.dumps(k) + ' = ' + toml(v) for k, v in value.items()) + '}'
    if isinstance(value, list): return '[' + ', '.join(toml(v) for v in value) + ']'
    return json.dumps(value, ensure_ascii=False)


def read_toml(path):
    try: import tomllib
    except ImportError: import tomli as tomllib
    return tomllib.loads(Path(path).read_text()) if Path(path).is_file() else {}


def read_native_json(path):
    # Copilot accepts JSONC. Do not silently drop authentication configuration
    # just because comments precede its first JSON object.
    text = Path(path).read_text(encoding='utf-8-sig')
    string = r'"(?:\\.|[^"\\])*"'
    text = re.sub('(' + string + r')|//[^\r\n]*|/\*.*?\*/',
                  lambda m: m[1] or ' ', text, flags=re.S)
    text = re.sub('(' + string + r')|,(?=\s*[}\]])', lambda m: m[1] or '', text)
    return json.loads(text)


@contextmanager
def supervised_process(command):
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, bufsize=1, start_new_session=True)
    try:
        yield process
    finally:
        if process.poll() is None:
            process.terminate()
            try: process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
        for stream in (process.stdin, process.stdout, process.stderr):
            try: stream.close()
            except (OSError, BrokenPipeError): pass


class AgentSandbox:
    """CLI backends see only the candidate and private native session state.

    Model endpoints use the configured proxy through Docker's host gateway.
    Verification runs separately without networking or provider credentials.
    """
    def __init__(self, root, image, *, kind='codex', evidence=None, provider_name='', environment=None, binding=''):
        self.root, self.image, self.kind = Path(root), image, kind
        self.provider_name = provider_name
        self._environment = None if environment is None else dict(environment)
        self.binding = binding
        self.evidence = Path(evidence).resolve() if evidence is not None else None
        self.recovery_evidence = None

    @property
    def environment(self):
        return dict(os.environ) if self._environment is None else self._environment

    @environment.setter
    def environment(self, values):
        self._environment = dict(values)

    def home(self, role):
        from .storage import require_space
        from .store import atomic_json
        path = self.root / ('reviewer' if role == 'review' else 'writer')
        marker = path / 'native-home.json'
        identity = {'version': 3, 'kind': self.kind, 'provider': self.provider_name,
                    'environment_binding': self.binding}
        if marker.is_file():
            if json.loads(marker.read_text()) != identity:
                raise RepairBlocked('provider_configuration', 'native session provider changed')
            return path
        require_space(path)
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        original = Path(self.environment.get('HOME', str(Path.home())))
        # Exact inputs only. profiles/ also contains native session databases,
        # transcripts and logs; recursive copying amplifies them every phase.
        names = {
            'codex': ['.codex/auth.json', '.codex/config.toml', '.codex/AGENTS.md'],
            'claude-code': ['.claude/.credentials.json', '.claude/settings.json', '.claude.json'],
            'copilot-cli': ['.copilot/config.json', '.copilot/settings.json'],
            'antigravity': ['.gemini/antigravity-cli/settings.json',
                           '.gemini/antigravity-cli/antigravity-oauth-token'],
        }.get(self.kind)
        if names is None:
            raise RepairBlocked('provider_unsupported', self.kind)
        if self.kind == 'codex':
            profile_home = Path(self.environment.get('CODEX_HOME') or original / '.codex').expanduser()
            names.extend('.codex/' + entry.name for entry in profile_home.glob('*.config.toml')
                         if entry.is_file() and not entry.is_symlink())
        for name in names:
            source, dest = original / name, path / name
            override = {'.codex': 'CODEX_HOME', '.claude': 'CLAUDE_CONFIG_DIR',
                        '.copilot': 'COPILOT_HOME'}.get(Path(name).parts[0])
            if override and self.environment.get(override):
                source = Path(self.environment[override]).expanduser() / Path(*Path(name).parts[1:])
            if not source.is_file(): continue
            if source.is_symlink() or any(p.is_symlink() for p in source.parents if p != original and original in p.parents):
                raise RepairBlocked('provider_configuration', 'native configuration must not traverse a link: ' + name)
            if source.stat().st_size > 1024 * 1024:
                raise RepairBlocked('provider_configuration', 'native configuration exceeds 1 MiB: ' + name)
            dest.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            # Preserve credentials refreshed by an interrupted native turn.
            if dest.exists(): continue
            if source.suffix == '.toml':
                config = read_toml(source)
                for key in ('mcp_servers', 'hooks', 'plugins', 'projects'): config.pop(key, None)
                dest.write_text('\n'.join(json.dumps(k) + ' = ' + toml(v) for k, v in config.items()) + '\n')
            elif source.suffix == '.json':
                config = read_native_json(source)
                if isinstance(config, dict):
                    for key in ('mcpServers', 'mcp_servers', 'hooks', 'plugins', 'enabledPlugins', 'projects'):
                        config.pop(key, None)
                dest.write_text(json.dumps(config))
            else:
                shutil.copyfile(source, dest)
            dest.chmod(0o600)
        atomic_json(marker, identity)
        return path

    @contextmanager
    def command(self, role, root, arguments, *, credential_names=()):
        from .docker import run
        from .cleanup import labels, reap_containers
        from .storage import execution_lease
        lease = 'reviewer' if role == 'review' else 'writer'
        reap_containers(self.root, kind='provider')
        with execution_lease(self.root / 'leases' / lease):
            root = Path(root).resolve()
            home = self.home(role).resolve()
            name = 'aa-agent-' + uuid.uuid4().hex
            argv = ['docker', 'run', '--init', '--name', name, *labels(self.root, lease, kind='provider'), '-i', '--read-only', '--network', 'bridge',
                '--user', f'{os.getuid()}:{os.getgid()}', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                '--pids-limit', '512', '--memory', '2g', '--tmpfs', '/tmp:rw,nosuid,exec,mode=1777,size=4g',
                '--tmpfs', '/run:rw,nosuid,mode=1777', '--workdir', str(root),
                '--mount', 'type=bind,src=' + str(root) + ',dst=' + str(root) + (',readonly' if role != 'implement' else ''),
                '--mount', 'type=bind,src=' + str(home) + ',dst=/agent-home',
                '-e', 'HOME=/agent-home', '-e', 'PYTHONDONTWRITEBYTECODE=1']
            if (root / '.git').exists():
                argv += ['--mount', 'type=bind,src=' + str(root / '.git') + ',dst=' + str(root / '.git') + ',readonly']
            if self.evidence is not None:
                argv += ['--mount', f'type=bind,src={self.evidence},dst=/repair-evidence,readonly']
            if role == 'review' and self.recovery_evidence is not None:
                argv += ['--mount', f'type=bind,src={self.recovery_evidence},dst=/repair-recovery/boundary.json,readonly']
            # Executables are public, read-only tool inputs; no host home is mounted.
            mounts = set()
            selected = {'codex': None, 'claude-code': 'claude', 'copilot-cli': 'copilot', 'antigravity': 'agy'}[self.kind]
            if selected:
                found = shutil.which(selected, path=self.environment.get('PATH'))
                if found:
                    found = Path(found)
                    # Bind the selected executable, not its enclosing HOME/bin or
                    # node prefix (which may also contain unrelated account config).
                    mounts.add(found)
            for path in sorted(mounts):
                argv += ['--mount', 'type=bind,src=' + str(path.resolve()) + ',dst=' + str(path) + ',readonly']
            if self.kind == 'antigravity':
                for leaf in ('bin', 'builtin'):
                    public = Path(self.environment.get('HOME', str(Path.home()))) / '.gemini/antigravity-cli' / leaf
                    if public.is_dir() and not public.is_symlink():
                        argv += ['--mount', f'type=bind,src={public},dst=/agent-home/.gemini/antigravity-cli/{leaf},readonly']
            native_home = {'codex': ('CODEX_HOME', '/agent-home/.codex'),
                           'claude-code': ('CLAUDE_CONFIG_DIR', '/agent-home/.claude'),
                           'copilot-cli': ('COPILOT_HOME', '/agent-home/.copilot')}.get(self.kind)
            if native_home:
                argv += ['-e', native_home[0] + '=' + native_home[1]]
            # Keep the image's pinned Python/toolchain PATH. Native drivers
            # invoke their selected CLI by absolute path; host CLI directories
            # must not replace the verification environment inside the image.
            # A provider must not receive another provider's account credentials.
            prefixes = {'codex': ('OPENAI_', 'CODEX_'), 'claude-code': ('ANTHROPIC_',),
                        'copilot-cli': ('COPILOT_',), 'antigravity': ('GOOGLE_', 'GEMINI_', 'ANTIGRAVITY_')}[self.kind]
            extra = set(credential_names)
            extra.update(getattr(self, 'configured_environment_keys', ()))
            extra.difference_update({'HOME', 'PATH', 'CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'COPILOT_HOME'})
            if self.kind == 'copilot-cli': extra.update(('GH_TOKEN', 'GITHUB_TOKEN'))
            if self.kind == 'claude-code': extra.add('CLAUDE_CODE_OAUTH_TOKEN')
            for key in self.environment:
                if key in {'HOME', 'PATH', 'CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'COPILOT_HOME'}:
                    continue
                proxy = key.lower() in ('http_proxy', 'https_proxy', 'no_proxy', 'all_proxy')
                if key.startswith(prefixes) or key in extra or proxy:
                    value = self.environment[key]
                    if (proxy and key.lower() != 'no_proxy') or key.lower().endswith('base_url'):
                        value = bridge_url(value)
                    if key.lower() == 'no_proxy': value += ',host.docker.internal'
                    argv += ['-e', key + '=' + value]
            try: yield [*argv, self.image, *arguments]
            finally:
                code, detail = run(['docker', 'rm', '-f', name], timeout=15)
                if code and not any(text in detail.lower() for text in ('no such container', 'no such object')):
                    raise RepairBlocked('provider_cleanup_failed',
                        'provider container could not be stopped; candidate retained for recovery')


class NativeDriver:
    def __init__(self, config, sandbox, *, effort='max', review_effort=None, timeout=1800):
        self.config, self.sandbox, self.effort, self.timeout = config, sandbox, effort, timeout
        self.review_effort = review_effort or effort
        from ..provider_environment import effective_environment, environment_binding
        self.environment = effective_environment(config)
        self.binding = environment_binding(config)
        self.binary = shutil.which(config.binary, path=self.environment.get('PATH'))
        if sandbox is not None:
            sandbox.kind = config.kind
            sandbox.provider_name = config.provider_name
            sandbox.environment = self.environment
            sandbox.binding = self.binding
            sandbox.configured_environment_keys = set(config.environment)
        if not self.binary: raise RepairBlocked('provider_missing', config.binary + ' is unavailable')
        if config.kind not in ('codex', 'claude-code', 'copilot-cli', 'antigravity'):
            raise RepairBlocked('provider_unsupported', 'V2 has no native driver for ' + config.kind)

    def set_review_evidence(self, path):
        if self.sandbox is None:
            return False
        self.sandbox.recovery_evidence = Path(path).resolve() if path is not None else None
        return path is not None

    def identity(self):
        from .store import digest
        import hashlib
        binary = Path(self.binary).resolve()
        value = hashlib.sha256()
        with binary.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''): value.update(chunk)
        model, settings = self.selected()
        # Hash transport/model semantics, never persist native credentials.
        return digest({'kind': self.config.kind, 'provider': self.config.provider_name,
                       'environment_binding': self.binding, 'binary': value.hexdigest(), 'model': model,
                       'settings': settings, 'arguments': self.config.extra_args, 'effort': self.effort,
                       'review': self.selected('review'), 'review_effort': self.review_effort})

    def preflight(self, root):
        """Probe native protocol support without spending a model turn."""
        from .docker import run
        with self.sandbox.command('plan', root, ['/usr/bin/codex' if self.config.kind == 'codex' else self.binary, '--help']) as command:
            code, help_text = run(command, timeout=30)
        required = {'codex': ('app-server',), 'claude-code': ('--output-format', '--permission-mode', '--resume'),
                    'copilot-cli': ('--mode', '--output-format', '--resume'),
                    'antigravity': ('--mode', '--output-format', '--conversation')}
        if code or any(flag not in help_text for flag in required[self.config.kind]):
            raise RepairBlocked('provider_configuration',
                self.config.kind + ' CLI lacks required native plan/resume support: ' + help_text[-1500:])
        self.arguments('plan', 'capability probe', '', None)
        return {'provider': self.config.kind, 'identity': self.identity()}

    def selected(self, role=None):
        effort = self.review_effort if role in ('plan', 'review') else self.effort
        profile = self.config.profile_map.get(effort, '')
        from ..prompting.runtime import last_option, _toml_overrides
        explicit_model = last_option(self.config.extra_args, '--model', '-m')
        if self.config.kind == 'codex':
            home = Path(self.environment.get('CODEX_HOME') or
                        Path(self.environment.get('HOME', str(Path.home()))) / '.codex')
            profile = last_option(self.config.extra_args, '--profile', '-p') or profile
            values = {**read_toml('/etc/codex/config.toml'), **read_toml(home / 'config.toml')}
            values.update(values.get('profiles', {}).get(profile, {}))
            values.update(read_toml(home / (profile + '.config.toml')))
            for key, value in _toml_overrides(self.config.extra_args).items():
                parts = key.split('.')
                target = values
                for part in parts[:-1]: target = target.setdefault(part, {})
                target[parts[-1]] = value
            if explicit_model: values['model'] = explicit_model
            return values.get('model', ''), values
        if self.config.kind == 'copilot-cli':
            from ..adapters.copilot_cli import CopilotCliAdapter
            adapter = CopilotCliAdapter(self.config)
            directory = adapter._resolve_config_dir(effort, Path.cwd())
            return explicit_model or (adapter._load_model_from_config_dir(directory) if directory else ''), {}
        return explicit_model or profile, {}

    def arguments(self, role, prompt, session, schema):
        model, settings = self.selected(role)
        kind = self.config.kind
        forbidden = ('--mode', '--permission-mode', '--resume', '--conversation', '--allow-all',
                     '--dangerously-skip-permissions', '--dangerously-bypass-approvals-and-sandbox',
                     '--sandbox', '--output-format', '--json-schema', '--output-schema', '--mcp-config')
        for flag in self.config.extra_args:
            if any(flag == key or flag.startswith(key + '=') for key in forbidden):
                raise RepairBlocked('provider_configuration', 'phase-conflicting provider option: ' + flag)
        if kind == 'codex':
            # Model/config overrides are carried in thread/start. Only native
            # app-server transport flags belong to this process invocation.
            return ['/usr/bin/codex', 'app-server', '--stdio']
        if kind == 'claude-code':
            args = [self.binary, '-p', '--output-format', 'stream-json', '--verbose',
                '--permission-mode', 'plan' if role == 'plan' else 'bypassPermissions',
                '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}']
            if session: args += ['--resume', session]
            if schema: args += ['--json-schema', json.dumps(schema)]
        elif kind == 'copilot-cli':
            args = [self.binary, '-p', prompt, '--mode', 'plan' if role == 'plan' else 'interactive',
                    '--output-format', 'json', '--no-ask-user', '--allow-all', '--no-auto-update']
            if session: args += ['--resume=' + session]
        else:
            args = [self.binary, '--mode', 'plan' if role == 'plan' else 'accept-edits',
                    '--output-format', 'stream-json', '--print-timeout', str(self.timeout) + 's',
                    '--dangerously-skip-permissions', '--print', prompt]
            if session: args += ['--conversation', session]
            if schema: args += ['--json-schema', json.dumps(schema)]
            # The selected native profile owns reasoning settings. Several agy
            # Gemini models encode effort in their model name and reject --effort.
        if model: args += ['--model', model]
        # Phase and isolation are controller-owned; do not permit extra_args to
        # override them. Model endpoints and auth stay in private native config.
        args += list(self.config.extra_args)
        return args

    def run(self, role, prompt, root, *, session='', schema=None, progress=None, cancel=None):
        from .storage import require_space
        require_space(root)
        self._next_space_check = 0
        args = self.arguments(role, prompt, session, schema)
        _, settings = self.selected(role)
        selected = settings.get('model_providers', {}).get(settings.get('model_provider', 'openai'), {})
        credential_names = (selected['env_key'],) if selected.get('env_key') else ()
        with self.sandbox.command(role, root, args, credential_names=credential_names) as command:
            with supervised_process(command) as process:
                messages = queue.Queue(maxsize=16)
                finished = threading.Event()
                def read(stream, label):
                    while not finished.is_set():
                        line = stream.readline(4 * 1024 * 1024)
                        item = (label, line if line else None)
                        while not finished.is_set():
                            try: messages.put(item, timeout=0.2); break
                            except queue.Full: pass
                        if not line: return
                for stream, label in ((process.stdout, 'stdout'), (process.stderr, 'stderr')):
                    threading.Thread(target=read, args=(stream, label), daemon=True).start()
                try:
                    if self.config.kind == 'codex':
                        return self.codex(process, messages, role, prompt, root, session, schema, progress, cancel)
                    if self.config.kind == 'claude-code': process.stdin.write(prompt + '\n'); process.stdin.flush()
                    process.stdin.close()
                    return self.cli(process, messages, progress, cancel)
                finally: finished.set()

    def next_message(self, process, messages, deadline, cancel):
        if time.monotonic() >= getattr(self, '_next_space_check', 0):
            from .storage import require_space
            require_space(self.sandbox.root)
            self._next_space_check = time.monotonic() + 5
        if cancel is not None and cancel.is_set():
            process.terminate(); raise InterruptedError('provider turn cancelled')
        if time.monotonic() > deadline:
            process.terminate(); raise TimeoutError('provider call exceeded its configured time budget')
        try: return messages.get(timeout=0.2)
        except queue.Empty: return '', ''

    def cli(self, process, messages, progress, cancel):
        from collections import deque
        text, errors, session, usage, done = '', deque(maxlen=32), '', {}, set()
        terminal, success, failed = False, False, False
        deadline = time.monotonic() + self.timeout
        try:
            while len(done) < 2:
                stream, line = self.next_message(process, messages, deadline, cancel)
                if line is None: done.add(stream); continue
                if not line: continue
                if stream == 'stderr': errors.append(line[-2000:]); continue
                try: event = json.loads(line)
                except ValueError: errors.append('non-JSON provider output: ' + line[:200]); continue
                data = event.get('data') or {}
                kind = event.get('type', '')
                session = event.get('session_id') or event.get('conversation_id') or data.get('sessionId') or session
                if progress: progress({'session': session, 'event': kind})
                if kind in ('assistant', 'assistant.message'):
                    message = event.get('message', data)
                    content = message.get('content', '')
                    if isinstance(content, list): content = ''.join(p.get('text', '') for p in content if p.get('type') == 'text')
                    if isinstance(content, str) and content: text = content
                if kind == 'result':
                    terminal = True
                    failed = failed or bool(event.get('is_error', False))
                    success = not event.get('is_error', False) and event.get('status', 'SUCCESS') in ('SUCCESS', 'success', 'completed')
                    structured = event.get('structured_output')
                    text = json.dumps(structured) if structured is not None else event.get('result') or event.get('response') or text
                    usage = event.get('usage') or {}
                    if event.get('error'): errors.append(str(event['error']))
                if kind in ('session.end', 'assistant.turn_end'): terminal = True; success = True
                if kind in ('session.error', 'assistant.error', 'error'): failed = True; success = False; errors.append(str(data or event))
            code = process.wait(timeout=5)
            return AgentReply(code == 0 and terminal and success and not failed and bool(text.strip()), text, session,
                              '\n'.join(errors)[-4000:], usage)
        except (InterruptedError, TimeoutError) as error:
            return AgentReply(False, text, session, str(error), usage, isinstance(error, InterruptedError),
                              timed_out=isinstance(error, TimeoutError))

    def codex(self, process, messages, role, prompt, root, session, schema, progress, cancel):
        deadline = time.monotonic() + self.timeout
        from collections import deque
        request_id, pending, errors = 0, {}, deque(maxlen=32)
        final, thread, usage = '', session, {}
        def send(method, params, identity=None):
            nonlocal request_id
            packet = {'method': method, 'params': params}
            if identity is None:
                request_id += 1; identity = request_id; pending[identity] = method
            packet['id'] = identity
            process.stdin.write(json.dumps(packet) + '\n'); process.stdin.flush()
        send('initialize', {'clientInfo': {'name': 'auto_agents_repair_v2', 'version': '2'},
                            'capabilities': {'experimentalApi': True}})
        model, settings = self.selected(role)
        for key in ('mcp_servers', 'hooks', 'plugins'): settings.pop(key, None)
        for provider in settings.get('model_providers', {}).values():
            if provider.get('base_url'): provider['base_url'] = bridge_url(provider['base_url'])
        settings['mcp_servers'] = {}
        settings.setdefault('features', {})['apps'] = False
        try:
            while True:
                stream, line = self.next_message(process, messages, deadline, cancel)
                if line is None:
                    if stream == 'stdout': return AgentReply(False, final, thread, '\n'.join(errors)[-4000:] or 'app server exited before completion')
                    continue
                if not line: continue
                if stream == 'stderr': errors.append(line[-2000:]); continue
                try: event = json.loads(line)
                except ValueError: continue
                if 'id' in event and 'method' not in event:
                    method = pending.pop(event['id'], '')
                    if event.get('error'):
                        detail = str(event['error'])
                        missing = method == 'thread/resume' and bool(re.search(r'(thread|session).*(not found|does not exist|unknown)|unknown (thread|session)', detail, re.I))
                        return AgentReply(False, final, thread, detail, missing_session=missing)
                    if method == 'initialize':
                        process.stdin.write('{"method":"initialized","params":{}}\n'); process.stdin.flush()
                        # The external container enforces phase-specific mounts.
                        # Avoid adding a second, incompatible namespace sandbox.
                        params = {'cwd': str(root), 'approvalPolicy': 'never',
                                  'sandbox': 'danger-full-access', 'config': settings}
                        if model: params['model'] = model
                        if thread:
                            params['threadId'] = thread
                            params['excludeTurns'] = True
                        send('thread/resume' if thread else 'thread/start', params)
                    elif method in ('thread/start', 'thread/resume'):
                        thread = event['result']['thread']['id']
                        if progress: progress({'session': thread, 'event': 'session'})
                        selected_model = model or event['result'].get('model')
                        if not selected_model: return AgentReply(False, '', thread, 'Codex model selection is unavailable')
                        params = {'threadId': thread, 'input': [{'type': 'text', 'text': prompt}],
                            'collaborationMode': {'mode': 'plan' if role == 'plan' else 'default',
                                'settings': {'model': selected_model,
                                    'reasoning_effort': settings.get('model_reasoning_effort', 'high'),
                                    'developer_instructions': None}}}
                        if schema: params['outputSchema'] = schema
                        send('turn/start', params)
                    continue
                method, params = event.get('method', ''), event.get('params') or {}
                if 'id' in event:
                    # Missing user facts must be surfaced, not guessed as authorization.
                    questions = params.get('questions') or []
                    detail = '; '.join(str(q.get('question') or q.get('header') or '') for q in questions if isinstance(q, dict))
                    return AgentReply(False, final, thread, 'provider requires external input: ' + method + (': ' + detail if detail else ''))
                if progress: progress({'session': thread, 'event': method})
                if method == 'item/completed':
                    item = params.get('item', {})
                    if item.get('type') == 'plan' or item.get('type') == 'agentMessage' and item.get('phase') in (None, 'final_answer'):
                        if item.get('text'): final = item['text']
                if method == 'thread/tokenUsage/updated': usage = params.get('tokenUsage', {})
                if method == 'turn/completed':
                    turn = params['turn']
                    return AgentReply(turn.get('status') == 'completed' and not turn.get('error') and bool(final.strip()), final, thread,
                                      str(turn.get('error') or ''), usage)
        except (InterruptedError, TimeoutError) as error:
            return AgentReply(False, final, thread, str(error), usage, isinstance(error, InterruptedError),
                              timed_out=isinstance(error, TimeoutError))
        finally:
            process.terminate()
