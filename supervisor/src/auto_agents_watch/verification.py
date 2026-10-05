"""Fixed baseline checks and the public offline recovery boundary."""
from pathlib import Path
import json
import subprocess
import shutil
import tempfile
import xml.etree.ElementTree as ET

from .git_delivery import git
from .store import atomic, digest


class Verifier:
    def __init__(self, source, base, root, sandbox):
        self.source, self.base, self.root, self.sandbox = Path(source), base, Path(root), sandbox
        self.baseline = self.root / 'checks'
        self.baseline.mkdir(parents=True,exist_ok=True)
        # The list and original test bytes come from the admission commit.
        self.files = [name for name in git(source,'ls-tree','-r','--name-only',base).splitlines()
                      if name.startswith('tests/') or name in {'conftest.py','pytest.ini'}]
        for name in self.files:
            path = self.baseline / name; path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(subprocess.check_output(['git','-C',str(source),'show',base + ':' + name]))
        # Docker must find the nested mountpoint in the read-only test tree.
        (self.baseline/'tests/repair').mkdir(parents=True,exist_ok=True)
        self.test_files = [name for name in self.files if name.endswith('.py') and Path(name).name.startswith('test_')]
        if not self.test_files: raise RuntimeError('No fixed regression tests in the admission revision')
        atomic(self.root/'verification-manifest.json',{'base':base,'files':self.files,'tests':self.test_files})

    def scope(self, candidate, *, against=None):
        changed = git(candidate,'diff','--name-only',against or self.base).splitlines()
        changed += git(candidate,'ls-files','--others','--exclude-standard').splitlines()
        forbidden = [name for name in set(changed) if not name.startswith(('src/auto_agents/','schemas/','tests/repair/'))
                     or name in self.files]
        regression_changes=git(candidate,'diff','--name-only',self.base).splitlines()+git(candidate,'ls-files','--others','--exclude-standard').splitlines()
        if changed and not any(name.startswith('tests/repair/') and name.endswith('.py') for name in regression_changes):
            forbidden.append('missing regression test under tests/repair/')
        return {'ok':not forbidden,'paths':forbidden}

    def recover(self, candidate, snapshot, token):
        # Recovery mutates only a disposable copy. Its input snapshot and
        # diagnostic reports remain available for a stopped task to resume.
        with tempfile.TemporaryDirectory(prefix='recovery-', dir=self.root) as temporary:
            private = Path(temporary) / 'project'
            shutil.copytree(snapshot,private,symlinks=True)
            # Materialized records make the copy self-contained; live paths are not mounted.
            token_file=self.root/'resume-token.json'; atomic(token_file,token)
            output=self.root/'recovery-result'; log=self.root/'recovery.log'
            result=self.sandbox.verify(['python','-m','auto_agents','resume-check','--project',token['project'],
                '--resume-token','/evidence/resume-token.json'],candidate,self.root,output,log,project=private)
            value=None
            for line in log.read_text(errors='replace').splitlines():
                try: row=json.loads(line)
                except ValueError: continue
                if isinstance(row,dict) and 'blocked_step_cleared' in row: value=row
            passed=result['ok'] and value and value.get('ok') and value.get('external_calls')==0
            status='passed' if passed else 'failed'
            if not value or result.get('reason') or value.get('category') in {'state','environment','reconciliation'}:
                status='unknown'
            return {'id':'original-boundary','status':status,'report':value,'log':str(log)}

    def regression(self,candidate):
        checks=[]
        mounts=[(self.baseline/'tests','/work/tests')]
        additions=Path(candidate)/'tests/repair'
        added_tests=[]
        if additions.is_dir():
            mounts.append((additions,'/work/tests/repair'))
            added_tests=[path.relative_to(candidate).as_posix() for path in sorted(additions.rglob('test_*.py'))]
        for name in ('conftest.py','pytest.ini'):
            if (self.baseline/name).exists(): mounts.append((self.baseline/name,'/work/'+name))
        for index,name in enumerate(self.test_files + added_tests):
            output=self.root/('test-result-'+str(index)); log=self.root/('test-'+str(index)+'.log')
            result=self.sandbox.verify(['python','-m','pytest','-q','-p','no:cacheprovider',
                '--junitxml=/result/junit.xml',name],
                candidate,self.root,output,log,extra_mounts=mounts)
            executed=0
            try:
                cases=list(ET.parse(output/'junit.xml').getroot().iter('testcase'))
                executed=sum(case.find('skipped') is None for case in cases)
            except (OSError,ET.ParseError): pass
            status='passed' if result['ok'] and executed else 'failed'
            if result.get('reason') or result['returncode'] in {125,126,127,137} or result['ok'] and not executed:
                status='environment'
            checks.append({'id':'regression:'+name,'status':status,'log':str(log),'executed':executed})
        return checks

    def verify(self,candidate,snapshot,token, *, scope_base=None):
        scope=self.scope(candidate, against=scope_base)
        if not scope['ok']:
            return {'ok':False,'checks':[{'id':'scope','status':'failed'}],'reason':'Protected paths modified','paths':scope['paths']}
        boundary=self.recover(candidate,snapshot,token)
        if boundary['status']!='passed':
            return {'ok':False,'checks':[boundary],
                    'reason':'Original checkpoint recovery failed'}
        checks=[boundary,*self.regression(candidate)]
        return {'ok':all(row['status']=='passed' for row in checks),'checks':checks,
                'revision':git(candidate,'rev-parse','HEAD')}
