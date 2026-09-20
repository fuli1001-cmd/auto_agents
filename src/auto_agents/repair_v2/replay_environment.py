"""Private, content-bound runtime inputs for the offline recovery preflight."""
from dataclasses import dataclass
import hashlib
import errno
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import tempfile

from .store import atomic_json, digest
from .storage import require_space
from .types import RepairBlocked


class EnvironmentUnavailable(RepairBlocked):
    def __init__(self, message):
        super().__init__('verification_infrastructure', message)


def commands(target, payload):
    """Only follow the retained root and its handoffs, not ambient old runs."""
    from ..execution_binding import route_sources
    target = Path(target)
    invocation = payload.get('invocation', {})
    pending = [('session', invocation.get('session_id'))]
    def routes(value):
        for source in route_sources(value):
            pending.extend(('handoff', source.get(key)) for key in (
                'failed_handoff_id', 'original_handoff_id', 'resume_handoff_id'))
            pending.append(('session', source.get('child_session_id')))
    routes(invocation.get('engine_route') or {})
    seen, result = set(), []
    workflow = invocation.get('workflow_id') or ''
    while pending:
        kind, identity = pending.pop(0)
        if not identity or (kind, identity) in seen:
            continue
        if not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,96}', identity):
            continue  # The real recovery entrypoint diagnoses invalid identities.
        seen.add((kind, identity))
        if len(seen) > 128:
            raise EnvironmentUnavailable('隔离验证无法解析过长的环境依赖交接链。')
        relative = (f'.auto-agents/state/sessions/{identity}/session_state.json' if kind == 'session'
                    else f'.auto-agents/state/handoffs/{identity}.json')
        path = target / relative
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(target.resolve()):
            continue
        data = json.loads(path.read_text())
        if workflow and data.get('workflow_id') and data['workflow_id'] != workflow:
            continue
        workflow = workflow or data.get('workflow_id', '')
        if kind == 'session':
            command = data.get('fix_verify_command')
            if isinstance(command, str) and command:
                result.append(command)
            pending.extend(('handoff', data.get(key)) for key in ('active_handoff_id', 'parent_handoff_id'))
        else:
            child = data.get('child') or {}
            if child.get('kind') in ('fix', 'collab', 'provider_resolve'):
                pending.append(('session', child.get('native_id')))
            routes(data.get('payload') or {})
    return list(dict.fromkeys(result))


def prefixes(command, project):
    from ..execution_binding import command_spans, ExecutionBindingError
    project = Path(project)
    cwd, result = project, []
    try:
        spans = command_spans(command)
    except ExecutionBindingError:
        return []  # Do not reinterpret opaque shell programs.
    for start, end in spans:
        words = shlex.split(command[start:end])
        while words and (words[0] in ('env', 'exec') or re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', words[0])):
            words.pop(0)
        if words[:1] == ['cd'] and len(words) == 2:
            cwd = Path(os.path.normpath(str(cwd / words[1])))
            continue
        if len(words) < 3 or Path(words[0]).name != 'conda' or words[1] != 'run':
            continue
        index = 2
        prefix = None
        while index < len(words) and words[index].startswith('-'):
            option = words[index]
            value = None
            if option in ('-p', '--prefix', '--cwd', '-n', '--name'):
                index += 1
                value = words[index] if index < len(words) else None
            elif '=' in option:
                option, value = option.split('=', 1)
            if option in ('-p', '--prefix'):
                prefix = value
            elif option in ('-n', '--name'):
                raise EnvironmentUnavailable('隔离验证需要明确的 Conda 环境路径，当前命令只指定了环境名称。')
            elif option == '--cwd' and value:
                if not Path(os.path.normpath(str(cwd / value))).is_relative_to(project):
                    raise EnvironmentUnavailable('隔离验证的工作目录超出当前项目。')
            index += 1
        if prefix is None:
            continue
        if not prefix or any(mark in prefix for mark in ('$', '`', '~')):
            raise EnvironmentUnavailable('隔离验证无法解析动态 Conda 环境路径。')
        path = Path(os.path.normpath(str(cwd / prefix)))
        if not path.is_relative_to(project) or path.name != '.conda' or any(c in str(path) for c in ',\n\r'):
            raise EnvironmentUnavailable('隔离恢复只复制当前项目中明确指定的 .conda 环境，不能覆盖保留的项目源码或状态。')
        result.append(path)
    return result


def inventory(root, *, node=False):
    """Canonical internal links can be relocated without reading outside inputs."""
    root = Path(root).resolve()
    label = 'Node 依赖' if node else 'Conda 环境'
    records, size = {}, 0
    for current, directories, files in os.walk(root, followlinks=False):
        if node:
            directories[:] = [name for name in directories
                              if name not in ('.npmrc', '.env') and not name.startswith('.env.')]
        # Activation state may contain credentials. Offline preflight uses the
        # actual interpreter/package files, never shell activation or providers.
        if Path(current) == root / 'etc/conda':
            directories[:] = [d for d in directories if d not in ('activate.d', 'deactivate.d')]
        for name in [*files, *(n for n in directories if (Path(current) / n).is_symlink())]:
            path = Path(current) / name
            relative = path.relative_to(root).as_posix()
            if node and (name == '.npmrc' or name == '.env' or name.startswith('.env.')):
                continue
            if relative == 'conda-meta/state':
                continue
            if path.is_symlink():
                resolved = path.resolve()
                if not resolved.is_relative_to(root):
                    raise EnvironmentUnavailable(label + '含有指向环境外部的链接，无法生成独立验证副本：' + relative)
                records[relative] = ['link', resolved.relative_to(root).as_posix()]
            elif path.is_file():
                stat = path.stat()
                size += stat.st_size
                value = hashlib.sha256()
                with path.open('rb') as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        value.update(chunk)
                records[relative] = ['file', value.hexdigest(), stat.st_mode & 0o777]
            else:
                raise EnvironmentUnavailable(label + '含有不能复制的特殊文件：' + relative)
    return records, size


@dataclass
class Snapshot:
    prefix: Path
    source: Path
    root: Path
    identity: str
    kind: str = 'conda-runtime-prefix'

    def describe(self):
        if self.kind == 'node-dependencies':
            return {'prefix': str(self.prefix), 'digest': self.identity, 'kind': self.kind,
                    'credential_files_excluded': True}
        return {'prefix': str(self.prefix), 'digest': self.identity, 'kind': 'conda-runtime-prefix',
                'activation_state_included': False}

    def verify(self):
        if (self.root.is_symlink() or self.prefix.resolve() != self.source
                or digest(inventory(self.root, node=self.kind == 'node-dependencies')[0]) != self.identity
                or digest(inventory(self.source, node=self.kind == 'node-dependencies')[0]) != self.identity):
            label = 'Node 依赖' if self.kind == 'node-dependencies' else 'Conda 环境'
            raise EnvironmentUnavailable('验证期间 ' + label + '发生变化，需重新生成隔离环境输入。')


def capture(cache, prefix, *, kind='conda-runtime-prefix'):
    cache = Path(cache).resolve()
    source = Path(prefix).resolve()
    node = kind == 'node-dependencies'
    label = 'Node 依赖' if node else 'Conda 环境'
    if kind not in {'conda-runtime-prefix', 'node-dependencies'}:
        raise EnvironmentUnavailable('未知的隔离验证依赖类型。')
    if node and (Path(prefix).name != 'node_modules' or not source.is_dir()):
        raise EnvironmentUnavailable('项目中实际的 Node 验证依赖不可用：' + str(prefix))
    if not node and (not (source / 'conda-meta/history').is_file() or not (source / 'bin/python').is_file()):
        raise EnvironmentUnavailable('项目中实际的 Conda 环境不可用：' + str(prefix))
    records, size = inventory(source, node=node)
    identity = digest(records)
    destination = Path(cache) / identity / 'prefix'
    if destination.is_symlink() or destination.parent.is_symlink():
        raise EnvironmentUnavailable('隔离 ' + label + '的存储路径被替换，不能用于验证。')
    if destination.exists():
        if digest(inventory(destination, node=node)[0]) != identity:
            raise EnvironmentUnavailable('保留的隔离 ' + label + '已被修改，不能用于验收。')
    else:
        require_space(Path(cache), size)
        Path(cache).mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix='preparing-', dir=cache))
        try:
            staged = temporary / 'prefix'
            staged.mkdir()
            for name, item in records.items():
                path = staged / name
                path.parent.mkdir(parents=True, exist_ok=True)
                if item[0] == 'link':
                    path.symlink_to(os.path.relpath(staged / item[1], path.parent))
                else:
                    shutil.copy2(source / name, path)
            if inventory(staged, node=node)[0] != records or inventory(source, node=node)[0] != records:
                raise EnvironmentUnavailable('复制期间 ' + label + '发生变化，当前副本不能用于验证。')
            atomic_json(temporary / 'manifest.json', {'digest': identity, 'files': records})
            try:
                temporary.rename(destination.parent)
            except OSError as error:
                if error.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                    raise
                if destination.is_symlink() or digest(inventory(destination, node=node)[0]) != identity:
                    raise EnvironmentUnavailable('并发生成的隔离环境内容不一致。')
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    return Snapshot(Path(prefix), source, destination, identity, kind)


def node_prefixes(target, payload):
    """Provision declared discovery inputs without adopting their task scope."""
    from ..gate_execution import dependency_link_paths
    from ..execution_binding import test_invocations
    target, project = Path(target), Path(payload['project'])
    session_id = payload.get('invocation', {}).get('session_id')
    if not isinstance(session_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,96}', session_id):
        return []
    session_path = target / f'.auto-agents/state/sessions/{session_id}/session_state.json'
    if (not session_path.is_file() or session_path.is_symlink()
            or not session_path.resolve().is_relative_to(target.resolve())):
        return []
    session = json.loads(session_path.read_text())
    workflow = payload.get('invocation', {}).get('workflow_id')
    if workflow and session.get('workflow_id') and workflow != session['workflow_id']:
        return []
    steps, retained_commands = [], commands(target, payload)
    for relative, field in (('.auto-agents/config.json', 'gates'),
                            ('.auto-agents/state/task_plan.json', 'plan')):
        path = target / relative
        if not path.is_file():
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(target.resolve()):
            raise EnvironmentUnavailable('保留的验证配置离开了冻结现场。')
        data = json.loads(path.read_text())
        if field == 'gates':
            gates = data.get('gates', {})
            steps.extend(gates.get('steps', []))
            retained_commands.extend(gates.get('commands', []))
        else:
            steps.extend(data.get('verification_steps', []))
    required = any(step.get('runner') == 'vitest' for step in steps)
    retained_commands.extend(step['command'] for step in steps if step.get('command'))
    for command in retained_commands:
        try:
            required |= any(invocation.runner == 'vitest' for invocation in test_invocations(command))
        except ValueError:
            continue  # The original runner still diagnoses opaque commands.
    if not required:
        return []
    selected = [project / relative for relative in dependency_link_paths(target)
                if Path(relative).name == 'node_modules' and (project / relative).is_dir()]
    if not selected:
        raise EnvironmentUnavailable('保留的 Vitest 预检需要项目中已安装的 node_modules；离线恢复不安装或替换依赖。')
    if any(',' in str(path) or '\n' in str(path) or '\r' in str(path) for path in selected):
        raise EnvironmentUnavailable('Node 验证依赖路径不能安全挂载。')
    if any(not path.resolve().is_relative_to(project.resolve()) for path in selected):
        raise EnvironmentUnavailable('Node 验证依赖指向当前项目外部，不能捕获。')
    if not any((path / 'vitest/package.json').is_file() for path in selected):
        raise EnvironmentUnavailable('项目的 node_modules 中缺少 Vitest，不能执行离线恢复预检。')
    return selected


def prepare(cache, target, payload):
    project = Path(payload['project'])
    selected = {prefix for command in commands(target, payload) for prefix in prefixes(command, project)}
    try:
        nodes = node_prefixes(target, payload)
        return [*[capture(cache, prefix) for prefix in sorted(selected)],
                *[capture(Path(cache) / 'node', prefix, kind='node-dependencies') for prefix in nodes]]
    except (OSError, ValueError) as error:
        raise EnvironmentUnavailable('无法捕获保留的验证环境：' + str(error)) from error
