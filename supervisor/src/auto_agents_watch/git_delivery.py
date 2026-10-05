"""Git's native concurrency checks, isolated merges, and bounded publication."""
from pathlib import Path
import subprocess


def git(root, *args, check=True):
    result = subprocess.run(['git','-c','core.hooksPath=/dev/null','-C',str(root),*args],
                            capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or 'Git operation failed')
    return result.stdout.strip() if check else result


def clean(root):
    return not git(root, 'status', '--porcelain', '--untracked-files=all')


def prepare(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not clean(source):
        raise RuntimeError('Engine source has uncommitted changes; candidate admission preserves user work')
    branch = git(source,'branch','--show-current')
    if not branch: raise RuntimeError('Engine delivery requires a local branch')
    base = git(source,'rev-parse','HEAD')
    subprocess.run(['git','clone','--no-local','--no-hardlinks',str(source),str(destination)],
                   check=True, capture_output=True)
    git(destination,'checkout','--detach',base)
    return {'source':str(source),'branch':branch,'base':base}


def commit(candidate, message):
    git(candidate,'add','-A')
    if git(candidate,'diff','--cached','--quiet',check=False).returncode:
        git(candidate,'-c','user.name=auto-agents maintenance','-c','user.email=maintenance@localhost',
            'commit','-m',message)
    return git(candidate,'rev-parse','HEAD')


def deliver(binding, candidate):
    source = Path(binding['source'])
    if not clean(source) or git(source,'branch','--show-current') != binding['branch']:
        raise RuntimeError('Engine source changed or contains user edits; candidate retained')
    revision = git(candidate,'rev-parse','HEAD')
    current = git(source,'rev-parse','HEAD')
    if current == revision: return revision
    if current != binding['base']:
        raise RuntimeError('Engine branch advanced during validation; candidate retained')
    git(source,'fetch','--no-tags',str(candidate),revision)
    git(source,'merge','--ff-only',revision)
    return revision


def publish(candidate, remote, ref, validate, *, max_refreshes=1):
    if not remote or not ref: return {'state':'not_requested'}
    for refresh in range(max_refreshes + 1):
        fetched = git(candidate,'fetch','--no-tags',remote,ref,check=False)
        if fetched.returncode:
            return {'state':'pending','reason':fetched.stderr.strip()}
        upstream = git(candidate,'rev-parse','FETCH_HEAD')
        revision = git(candidate,'rev-parse','HEAD')
        if git(candidate,'merge-base','--is-ancestor',revision,upstream,check=False).returncode == 0:
            return {'state':'published','revision':revision,'remote_revision':upstream}
        if git(candidate,'merge-base','--is-ancestor',upstream,revision,check=False).returncode:
            # A failed merge remains in this private workspace for inspection.
            merged = git(candidate,'-c','user.name=auto-agents maintenance',
                         '-c','user.email=maintenance@localhost','merge','--no-edit',upstream,check=False)
            if merged.returncode:
                return {'state':'conflict','reason':merged.stderr or merged.stdout,
                        'paths':git(candidate,'diff','--name-only','--diff-filter=U').splitlines()}
            verdict = validate()
            if not verdict['ok']:
                return {'state':'pending','reason':'Merged revision failed verification or review','verification':verdict}
        revision = git(candidate,'rev-parse','HEAD')
        pushed = git(candidate,'push',remote,revision + ':' + ref,check=False)
        if pushed.returncode == 0:
            return {'state':'published','revision':revision}
        if not any(word in pushed.stderr for word in ('non-fast-forward','fetch first','rejected')):
            return {'state':'pending','reason':pushed.stderr.strip()}
    return {'state':'pending','reason':'Remote advanced repeatedly; publication retained'}
