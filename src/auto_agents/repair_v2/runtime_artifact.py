"""Portable, content-bound Git runtimes built before acceptance, never worktrees."""
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import tempfile

from .store import atomic_json, digest
from .storage import require_space, tree_bytes
from .types import RepairBlocked, RuntimeArtifact
from .workspace import git, inventory, source_identity


FORMAT = 'standalone-git-v1'


def inspect(root, *, commit=None, source=None):
    root = Path(root).resolve()
    metadata = root / '.git'
    try:
        if metadata.is_symlink() or not metadata.is_dir():
            raise ValueError('运行目录必须包含独立的 .git 目录')
        if (metadata / 'commondir').exists() or (metadata / 'objects/info/alternates').exists():
            raise ValueError('Git 对象不能依赖外部目录')
        if git(root, 'rev-parse', '--path-format=absolute', '--git-common-dir') != str(metadata):
            raise ValueError('Git 公共目录不属于运行产物')
        for path in metadata.rglob('*'):
            if path.is_symlink():
                raise ValueError('Git 元数据包含符号链接')
        actual = git(root, 'rev-parse', 'HEAD')
        if commit and actual != commit:
            raise ValueError('Git 提交与产物声明不一致')
        files = inventory(root)
        for name, row in files.items():
            if row[0] == 'link' and not (root / name).resolve().is_relative_to(root):
                raise ValueError('源码符号链接依赖运行产物外部路径: ' + name)
        identity = digest(files)
        if source and identity != source:
            raise ValueError('运行产物源码与验收输入不一致')
        return actual, identity
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise RepairBlocked('runtime_artifact_invalid', str(error)) from error


def build(root, source, identity, environments):
    source, root = Path(source), Path(root)
    commit = git(source, 'rev-parse', 'HEAD')
    inputs = {'format': FORMAT, 'commit': commit, 'source': identity, 'environments': environments}
    artifact_id = digest(inputs)
    destination = root / 'runtime-artifacts' / artifact_id
    manifest = destination / 'manifest.json'
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        require_space(destination.parent, tree_bytes(source) * 2)
        with tempfile.TemporaryDirectory(prefix='.building-', dir=destination.parent) as temporary:
            staging = Path(temporary) / 'artifact'
            staging.mkdir()
            checkout = staging / 'source'
            subprocess.run(['git', 'clone', '--quiet', '--no-local', '--no-hardlinks', '--no-checkout',
                            str(source), str(checkout)], check=True, capture_output=True)
            (checkout / '.git/config').write_text('[core]\n repositoryformatversion = 0\n bare = false\n')
            git(checkout, 'checkout', '--quiet', '--detach', commit)
            for name, row in inventory(source).items():
                if row[0] == 'file': (checkout / name).chmod(row[2])
            inspect(checkout, commit=commit, source=identity)
            if source_identity(source) != identity:
                raise RepairBlocked('source_changed', '源码在生成运行产物时发生变化')
            atomic_json(staging / 'manifest.json', {**inputs, 'artifact_id': artifact_id})
            staging.rename(destination)
    saved = json.loads(manifest.read_text())
    if saved != {**inputs, 'artifact_id': artifact_id}:
        raise RepairBlocked('runtime_artifact_invalid', '运行产物清单与预期不一致')
    artifact = RuntimeArtifact(artifact_id, str(destination / 'source'), commit, identity, environments, FORMAT)
    verify(asdict(artifact))
    return asdict(artifact)


def verify(artifact):
    try:
        _verify(artifact)
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise RepairBlocked('runtime_artifact_invalid', '运行产物元数据不完整或已改变: ' + str(error)) from error


def _verify(artifact):
    if artifact.get('format') != FORMAT or artifact.get('version') != 1:
        raise RepairBlocked('runtime_protocol', '运行产物格式不受当前控制器支持')
    expected = {key: artifact[key] for key in ('format', 'commit', 'source', 'environments')}
    if digest(expected) != artifact.get('artifact_id'):
        raise RepairBlocked('runtime_artifact_invalid', '运行产物身份校验失败')
    saved = json.loads((Path(artifact['path']).parent / 'manifest.json').read_text())
    if saved != {**expected, 'artifact_id': artifact['artifact_id']}:
        raise RepairBlocked('runtime_artifact_invalid', '运行产物清单已改变')
    inspect(artifact['path'], commit=artifact['commit'], source=artifact['source'])


def prepare(controller, source):
    """Used by both early recovery probes and centralized acceptance."""
    controller.phase('artifact')
    artifact = build(controller.store.root, source, controller.state['snapshot'],
                     {**getattr(controller, 'runtime_environments', {}),
                      'image': getattr(controller.verifier, 'image', ''),
                      'verifier': getattr(controller.verifier, 'runtime', '')})
    controller.checkpoint(runtime_artifact=artifact, snapshot_path=artifact['path'])
    preflight = getattr(controller.verifier, 'artifact_preflight', None)
    if preflight:
        from .proofs import once
        result = once(controller.store, 'artifact-preflight', {'artifact': artifact['artifact_id']},
                      lambda: preflight(artifact, controller.cancel))
        if not result.get('ok'):
            raise RepairBlocked('verification_infrastructure' if result.get('infrastructure') else
                                'runtime_artifact_invalid', result.get('reason') or '运行产物独立启动检查失败')
    return Path(artifact['path'])
