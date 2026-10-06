"""Release completed maintenance inputs; keep receipts and pending publication."""
from pathlib import Path
import hashlib
import os
import re
import shutil
import subprocess

from .process import alive
from . import git_delivery as delivery


def completed_job(store,job):
    if job.get('cleanup',{}).get('state')=='released':return job['cleanup']
    if job['state']!='DONE' or job.get('active_call') or alive(job.get('process')):
        return {'state':'retained','reason':'Task or model outcome is not settled'}
    episodes=[job,*job.get('history',[])]
    if any(isinstance(item.get('publication'),dict) and item['publication'].get('state') not in {'published','not_requested'} for item in episodes):
        return {'state':'retained','reason':'Publication still needs its original inputs'}
    directory=store.root/'jobs'/job['id'];sandbox=store.root/'sandboxes'/job['id']
    if sandbox.exists():
        owner=hashlib.sha256(str(sandbox.resolve()).encode()).hexdigest()
        probe=subprocess.run(['docker','ps','-aq','--filter','label=auto-agents-watch.owner='+owner],capture_output=True,text=True,timeout=15)
        if probe.returncode or probe.stdout.strip():
            return {'state':'retained','reason':'Owned containers have not quiesced'}
    candidates=[]
    for path in directory.iterdir():
        if re.fullmatch(r'candidate(?:-\d+)?',path.name):
            if path.is_symlink() or not delivery.clean(path):
                return {'state':'retained','reason':'Candidate contains uncommitted or unrecognized work'}
            candidates.append(path)
    # Preserve every candidate commit before releasing its working tree.
    for path in candidates:
        bundle=directory/(path.name+'.bundle')
        if not bundle.exists():
            temporary=bundle.with_suffix('.bundle.tmp')
            temporary.unlink(missing_ok=True)
            delivery.git(path,'bundle','create',str(temporary),'HEAD','--all')
            delivery.git(path,'bundle','verify',str(temporary))
            os.replace(temporary,bundle)
        delivery.git(path,'bundle','verify',str(bundle))
    released=[]
    for path in directory.iterdir():
        if re.fullmatch(r'(?:project|candidate|publication)(?:-\d+)?|publication-retry',path.name):
            if path.is_symlink() or not path.is_dir() or path.stat().st_uid!=os.getuid():
                raise ValueError('Maintenance input ownership changed')
            if path.name.startswith('publication') and not delivery.clean(path):
                return {'state':'retained','reason':'Publication has uncommitted work'}
    for path in list(directory.iterdir()):
        if re.fullmatch(r'(?:project|candidate|publication)(?:-\d+)?|publication-retry',path.name):
            shutil.rmtree(path);released.append(path.name)
        elif re.fullmatch(r'verification(?:-\d+)?',path.name) and path.is_dir() and not path.is_symlink():
            for private in list(path.glob('recovery-*')):
                if private.is_dir() and not private.is_symlink():
                    shutil.rmtree(private);released.append(str(private.relative_to(directory)))
    if sandbox.exists():
        if sandbox.is_symlink() or sandbox.stat().st_uid!=os.getuid():raise ValueError('Sandbox ownership changed')
        shutil.rmtree(sandbox);released.append('private-agent-homes')
    return {'state':'released','paths':released,'candidate_history':'verified Git bundles','reports_retained':True}
