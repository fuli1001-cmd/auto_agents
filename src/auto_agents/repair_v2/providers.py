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
    def __init__(self, root, image, *, kind='codex'):
        self.root, self.image, self.kind = Path(root), image, kind

    def home(self, role):
        from .storage import require_space
        from .store import atomic_json
        path = self.root / ('reviewer' if role == 'review' else 'writer')
        marker = path / 'native-home.json'
        if marker.is_file():
            if json.loads(marker.read_text()) != {'version': 2, 'kind': self.kind}:
                raise RepairBlocked('provider_configuration', 'native session provider changed')
            return path
        require_space(path)
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        original = Path.home()
        # Exact inputs only. profiles/ also contains native session databases,
        # transcripts and logs; recursive copying amplifies them every phase.
        names = {
            'codex': ['.codex/auth.json', '.codex/config.toml'],
            'claude-code': ['.claude/.credentials.json', '.claude/settings.json', '.claude.json'],
            'copilot-cli': ['.copilot/config.json', '.copilot/settings.json'],
            'antigravity': ['.gemini/antigravity-cli/settings.json',
                           '.gemini/antigravity-cli/antigravity-oauth-token'],
        }.get(self.kind)
        if names is None:
            raise RepairBlocked('provider_unsupported', self.kind)
        for name in names:
            source, dest = original / name, path / name
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
        atomic_json(marker, {'version': 2, 'kind': self.kind})
        return path

    @contextmanager
    def command(self, role, root, arguments):
        from .docker import run
        root = Path(root).resolve()
        home = self.home(role).resolve()
        name = 'aa-agent-' + uuid.uuid4().hex
        argv = ['docker', 'run', '--name', name, '-i', '--read-only', '--network', 'bridge',
            '--user', f'{os.getuid()}:{os.getgid()}', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
            '--pids-limit', '512', '--memory', '2g', '--tmpfs', '/tmp:rw,nosuid,mode=1777',
            '--tmpfs', '/run:rw,nosuid,mode=1777', '--workdir', str(root),
            '--mount', 'type=bind,src=' + str(root) + ',dst=' + str(root) + (',readonly' if role != 'implement' else ''),
            '--mount', 'type=bind,src=' + str(home) + ',dst=/agent-home',
            '-e', 'HOME=/agent-home', '-e', 'PYTHONDONTWRITEBYTECODE=1']
        if (root / '.git').exists():
            argv += ['--mount', 'type=bind,src=' + str(root / '.git') + ',dst=' + str(root / '.git') + ',readonly']
        # Executables are public, read-only tool inputs; no host home is mounted.
        mounts = set()
        for executable in ('node', 'codex', 'claude', 'copilot', 'agy'):
            found = shutil.which(executable)
            if not found: continue
            found = Path(found)
            resolved = found.resolve()
            if 'node_modules' in resolved.parts:
                index = resolved.parts.index('node_modules')
                mounts.add(Path(*resolved.parts[:index - 1]))  # node prefix
            else: mounts.add(resolved)
            if found != resolved: mounts.add(found.parent)
        for path in sorted(mounts):
            argv += ['--mount', 'type=bind,src=' + str(path) + ',dst=' + str(path) + ',readonly']
        prefixes = sorted({str(Path(shutil.which(n)).parent) for n in ('node', 'codex', 'claude', 'copilot', 'agy') if shutil.which(n)})
        argv += ['-e', 'PATH=' + ':'.join([*prefixes, '/usr/bin', '/bin'])]
        # Pass only model-auth/transport variables, never the business environment.
        for key in os.environ:
            if key.startswith(('OPENAI_', 'ANTHROPIC_', 'COPILOT_', 'GOOGLE_')) or key in (
                    'GH_TOKEN', 'GITHUB_TOKEN', 'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY',
                    'http_proxy', 'https_proxy', 'no_proxy', 'ALL_PROXY', 'all_proxy'):
                value = os.environ[key]
                if key.lower().endswith('_proxy') and key.lower() != 'no_proxy':
                    parsed = urlsplit(value)
                    if parsed.hostname in ('127.0.0.1', 'localhost'):
                        authority = parsed.netloc.replace(parsed.hostname, 'host.docker.internal')
                        value = urlunsplit((parsed.scheme, authority, parsed.path, parsed.query, parsed.fragment))
                    argv += ['-e', key + '=' + value]
                else: argv += ['-e', key]
        try: yield [*argv, self.image, *arguments]
        finally: run(['docker', 'rm', '-f', name], timeout=15)


class NativeDriver:
    def __init__(self, config, sandbox, *, effort='max', timeout=1800):
        self.config, self.sandbox, self.effort, self.timeout = config, sandbox, effort, timeout
        self.binary = shutil.which(config.binary)
        if sandbox is not None: sandbox.kind = config.kind
        if not self.binary: raise RepairBlocked('provider_missing', config.binary + ' is unavailable')
        if config.kind not in ('codex', 'claude-code', 'copilot-cli', 'antigravity'):
            raise RepairBlocked('provider_unsupported', 'V2 has no native driver for ' + config.kind)

    def selected(self):
        profile = self.config.profile_map.get(self.effort, '')
        if self.config.kind == 'codex':
            values = read_toml(Path.home() / '.codex/config.toml')
            values.update(read_toml(Path.home() / '.codex' / (profile + '.config.toml')))
            return values.get('model', ''), values
        if self.config.kind == 'copilot-cli':
            from ..adapters.copilot_cli import CopilotCliAdapter
            adapter = CopilotCliAdapter(self.config)
            directory = adapter._resolve_config_dir(self.effort, Path.cwd())
            return adapter._load_model_from_config_dir(directory) if directory else '', {}
        return profile, {}

    def arguments(self, role, prompt, session, schema):
        model, settings = self.selected()
        kind = self.config.kind
        if kind == 'codex': return [self.binary, 'app-server', '--stdio']
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
        for flag in self.config.extra_args:
            if flag.startswith(('--mode', '--permission', '--resume', '--conversation', '--allow', '--dangerously')):
                raise RepairBlocked('provider_configuration', 'phase-conflicting provider option: ' + flag)
        args += list(self.config.extra_args)
        return args

    def run(self, role, prompt, root, *, session='', schema=None, progress=None, cancel=None):
        from .storage import require_space
        require_space(root)
        self._next_space_check = 0
        args = self.arguments(role, prompt, session, schema)
        with self.sandbox.command(role, root, args) as command:
            with supervised_process(command) as process:
                messages = queue.Queue()
                def read(stream, label):
                    for line in iter(stream.readline, ''): messages.put((label, line))
                    messages.put((label, None))
                for stream, label in ((process.stdout, 'stdout'), (process.stderr, 'stderr')):
                    threading.Thread(target=read, args=(stream, label), daemon=True).start()
                if self.config.kind == 'codex':
                    return self.codex(process, messages, role, prompt, root, session, schema, progress, cancel)
                if self.config.kind == 'claude-code': process.stdin.write(prompt + '\n'); process.stdin.flush()
                process.stdin.close()
                return self.cli(process, messages, progress, cancel)

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
        text, errors, session, usage, done = '', [], '', {}, set()
        terminal, success = False, False
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
                    success = not event.get('is_error', False) and event.get('status', 'SUCCESS') in ('SUCCESS', 'success', 'completed')
                    structured = event.get('structured_output')
                    text = json.dumps(structured) if structured is not None else event.get('result') or event.get('response') or text
                    usage = event.get('usage') or {}
                    if event.get('error'): errors.append(str(event['error']))
                if kind in ('session.end', 'assistant.turn_end'): terminal = True; success = True
                if kind in ('session.error', 'assistant.error', 'error'): success = False; errors.append(str(data or event))
            code = process.wait(timeout=5)
            return AgentReply(code == 0 and terminal and success and bool(text.strip()), text, session,
                              '\n'.join(errors)[-4000:], usage)
        except (InterruptedError, TimeoutError) as error:
            return AgentReply(False, text, session, str(error), usage, isinstance(error, InterruptedError))

    def codex(self, process, messages, role, prompt, root, session, schema, progress, cancel):
        deadline = time.monotonic() + self.timeout
        request_id, pending, errors = 0, {}, []
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
        model, settings = self.selected()
        for key in ('mcp_servers', 'hooks', 'plugins'): settings.pop(key, None)
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
                    if event.get('error'): return AgentReply(False, final, thread, str(event['error']))
                    if method == 'initialize':
                        process.stdin.write('{"method":"initialized","params":{}}\n'); process.stdin.flush()
                        # The external container enforces phase-specific mounts.
                        # Avoid adding a second, incompatible namespace sandbox.
                        params = {'cwd': str(root), 'approvalPolicy': 'never',
                                  'sandbox': 'danger-full-access', 'config': settings}
                        if model: params['model'] = model
                        if thread: params['threadId'] = thread
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
                    return AgentReply(False, final, thread, 'provider requires external input: ' + method)
                if progress: progress({'session': thread, 'event': method})
                if method == 'item/completed':
                    item = params.get('item', {})
                    if item.get('type') == 'plan' or item.get('type') == 'agentMessage' and item.get('phase') in (None, 'final_answer'):
                        if item.get('text'): final = item['text']
                if method == 'thread/tokenUsage/updated': usage = params.get('tokenUsage', {})
                if method == 'turn/completed':
                    turn = params['turn']
                    return AgentReply(turn.get('status') == 'completed' and bool(final.strip()), final, thread,
                                      str(turn.get('error') or ''), usage)
        except (InterruptedError, TimeoutError) as error:
            return AgentReply(False, final, thread, str(error), usage, isinstance(error, InterruptedError))
        finally:
            process.terminate()
