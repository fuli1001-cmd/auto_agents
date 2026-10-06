"""Six-state maintenance: one candidate, fixed gates, and normal Git delivery."""
from pathlib import Path
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import re

from .store import Store, atomic, digest
from .process import run, alive
from .providers import selection, Driver
from .sandbox import Docker
from .verification import Verifier
from . import git_delivery as delivery


def task_arguments(argv):
    """Provider/launcher changes do not create new credit for the same task."""
    args=list(argv)
    start=args.index('auto_agents')+1 if 'auto_agents' in args else 1
    args=args[start:]
    normalized=[];index=0
    while index<len(args):
        arg=args[index]
        if arg=='--provider':index+=2;continue
        if arg.startswith('--provider=') or arg in {'--no-supervisor','--print-agent-output'}:
            index+=1;continue
        normalized.append(arg);index+=1
    return normalized


def normalized_error(message):
    value=' '.join(str(message).split())
    value=re.sub(r'/[^\s\"\']+','<path>',value)
    return re.sub(r'\b[0-9a-f]{8,}\b','<id>',value)


def review_prompt(job, report):
    return ('Independently review the immutable candidate, including any merged changes, '
        'against the original fault and fixed checks. Require a generic engine fix and '
        'a regression test; reject project/provider special cases, weakened checks, '
        'fabricated completion, changed goals or authorization, and scope expansion. '
        'Return only JSON {"ok":true|false,"reason":"..."}.\n'
        +json.dumps({'fault':job['fault'],'checkpoint':job['resume_token'],
                    'verification':report},ensure_ascii=False))


class Runner:
    def __init__(self,store=None,*,sandbox_factory=Docker,driver_factory=Driver,verifier_factory=Verifier):
        self.store=store or Store()
        self.sandbox_factory,self.driver_factory,self.verifier_factory=sandbox_factory,driver_factory,verifier_factory

    @contextmanager
    def locked(self,job):
        path=Path(job['project'])/'.auto-agents/state/run.lock'
        path.parent.mkdir(parents=True,exist_ok=True)
        fd=os.open(path,os.O_RDWR|os.O_CREAT,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.fd=fd
            self.token=digest([job['id'],time.time()])
            from .process import process_identity
            atomic_payload={'version':3,'pid':os.getpid(),'pid_start_ticks':int(process_identity(os.getpid())[1]),
                            'project':job['project'],'run_token':self.token}
            os.ftruncate(fd,0)
            os.lseek(fd,0,os.SEEK_SET)
            os.write(fd,json.dumps(atomic_payload).encode()); os.fsync(fd)
            yield
        finally:
            os.close(fd)

    def env(self,job):
        environment = dict(os.environ)
        if job.get('runtime_source'):
            environment.update(PYTHONPATH=str(Path(job['runtime_source'])/'src'),
                               AUTO_AGENTS_PINNED_RUNTIME=job['candidate_revision'])
        if job.get('runtime_argv'):
            environment['AUTO_AGENTS_MAINTENANCE_RESUME']='1'
        return {**environment,'AUTO_AGENTS_NO_SUPERVISOR':'1','AUTO_AGENTS_ENGINE_SOURCE_ROOT':job['engine'],
                'AUTO_AGENTS_OBSERVATION_FILE':str(self.directory/'observation.json'),
                'AUTO_AGENTS_RUN_LOCK_FD':str(self.fd),'AUTO_AGENTS_RUN_TOKEN':self.token,
                'AUTO_AGENTS_RUN_LOCK_KEY':hashlib.sha256(job['project'].encode()).hexdigest()}

    def start(self,argv,engine):
        project=None
        for i,arg in enumerate(argv):
            if arg=='--project' and i+1<len(argv): project=argv[i+1]
            elif arg.startswith('--project='): project=arg.split('=',1)[1]
        if not project: raise ValueError('Supervised business commands require --project')
        normalized_project = str(Path(project).resolve())
        # Re-running the same maintenance task must not replenish its budget.
        existing = [job for job in self.store.list() if job['project'] == normalized_project
                    and job['engine']==str(Path(engine).resolve())
                    and task_arguments(job['argv'])==task_arguments(argv) and job['state'] != 'DONE']
        if existing:
            retained=max(existing,key=lambda job:job['created'])
            if alive(retained.get('process')) or retained.get('active_call'):
                return self.resume(retained['id'])
            self.store.save(retained,argv=list(argv))
            return self.resume(retained['id'])
        job=self.store.create(argv,project,engine)
        return self.resume(job['id'])

    def configured(self,job):
        config=json.loads((Path(job['project'])/'.auto-agents/config.json').read_text())
        name,provider=selection(config,job['argv'])
        settings=config.get('execution',{}).get('supervision',{})
        return config,name,provider,settings

    def check_cancelled(self,job):
        if self.store.get(job['id']).get('cancel_requested'):
            raise RuntimeError('Cancelled; checkpoint and candidate retained')

    def business(self,job):
        self.check_cancelled(job)
        observation=self.directory/'observation.json'
        observation.unlink(missing_ok=True)
        self.store.save(job,'RUNNING')
        config_path=Path(job['project'])/'.auto-agents/config.json'
        config=json.loads(config_path.read_text()) if config_path.exists() else {}
        settings=config.get('execution',{}).get('supervision',{})
        result=run(job.get('runtime_argv') or job['argv'],cwd=job.get('cwd',job['engine']),env=self.env(job),
            log=self.directory/'business.log',observation=observation,pass_fds=(self.fd,),
            repeat_limit=settings.get('loop_repeat_limit',3),
            heartbeat_timeout=settings.get('heartbeat_timeout_seconds',120),stream=True,
            on_start=lambda process:self.store.save(job,process=process),
            cancelled=lambda:self.store.get(job['id']).get('cancel_requested',False))
        value=json.loads(observation.read_text()) if observation.exists() else {}
        self.store.save(job,process=None,observation=value)
        if self.store.get(job['id']).get('cancel_requested'):result['reason']='cancelled'
        if result['reason'] or value.get('fault'):
            cleanup=subprocess.run([sys.executable,'-m','auto_agents','quiesce','--project',job['project']],
                env=self.env(job),pass_fds=(self.fd,),capture_output=True,text=True,timeout=15)
            if cleanup.returncode:
                self.store.save(job,'STOPPED',reason='Owned business children have not quiesced; evidence retained')
                return False
        if result['reason']=='cancelled':
            self.store.save(job,'STOPPED',reason='Cancelled; checkpoint retained'); return False
        if value.get('status')=='completed' and result['returncode']==0:
            self.store.save(job,'DONE',reason='Original business task completed'); return False
        fault=value.get('fault')
        if result['reason']=='control_cycle':
            invocation=self.directory/'invocation.json'
            raw=job['argv']
            arguments=raw[raw.index('auto_agents')+1:] if 'auto_agents' in raw else raw[1:]
            atomic(invocation,arguments)
            proc=subprocess.run([sys.executable,'-m','auto_agents','checkpoint','--project',job['project'],
                '--observation',str(observation),'--invocation',str(invocation)],
                env={**os.environ,'PYTHONPATH':str(Path(job['engine'])/'src'),
                     'AUTO_AGENTS_NO_SUPERVISOR':'1'},capture_output=True,text=True)
            if proc.returncode == 0:
                fault=json.loads(proc.stdout)['fault']
        elif result['reason'] in {'heartbeat_lost','operation_timeout'}:
            self.store.save(job,'STOPPED',reason='Execution health is uncertain; checkpoint retained',execution=result)
            return False
        if not fault:
            self.store.save(job,'STOPPED',reason='Business task stopped without an engine defect',execution=result); return False
        if fault['category'] not in {'engine','unknown'}:
            self.store.save(job,'STOPPED',reason=fault['message'],fault=fault); return False
        if not fault.get('resume_token'):
            self.store.save(job,'STOPPED',reason='Fault lacks a durable resume checkpoint',fault=fault); return False
        token=json.loads(Path(fault['resume_token']).read_text())
        fingerprint=digest([fault['type'],fault['message'],str(value.get('phase','')).split(':')[0],value.get('subject')])
        sequence=value.get('progress_seq',0)
        if job.get('runtime_argv') and job.get('fault'):
            if (fingerprint==job.get('fault_fingerprint') or sequence<=job.get('fault_progress_seq',0)):
                self.store.save(job,'STOPPED',reason='Verified repair did not yield new business progress; new evidence retained',
                                subsequent_fault=fault)
                return False
            history=[*job.get('history',[]),{'fault':job['fault'],'candidate':job.get('candidate'),
                     'revision':job.get('candidate_revision'),'verification':job.get('verification'),
                     'publication':job.get('publication')}]
            self.store.save(job,history=history,episode=job.get('episode',0)+1,
                candidate=None,candidate_revision=None,snapshot=None,verification=None,
                original_reproduction_confirmed=False,best_passed=[],no_progress=0,publication='not_requested')
        self.store.save(job,fault=fault,resume_token=token,fault_fingerprint=fingerprint,
                        fault_progress_seq=sequence,needs_maintenance=True,
                        maintenance_started=job.get('maintenance_started') or time.time())
        return True

    def model(self,job,driver,role,prompt, *, candidate=None):
        _,_,_,settings=self.configured(job)
        limit=settings.get('max_duration_seconds')
        if limit and time.time()-job.get('maintenance_started',job['created'])>=limit:
            raise RuntimeError('Configured maintenance duration limit reached')
        self.store.reserve(job,role,max_calls=settings.get('max_model_calls'))
        try:
            limit=settings.get('max_duration_seconds')
            remaining=max(.1,limit-(time.time()-job.get('maintenance_started',job['created']))) if limit else None
            result=driver.call(role,Path(candidate or job['candidate']),self.directory,prompt,
                self.directory/(role+'-'+str(job['model_calls'])+'.log'),
                timeout=remaining,
                on_start=lambda process:self.store.save(job,process=process),
                cancelled=lambda:self.store.get(job['id']).get('cancel_requested',False))
            if result.get('confirmed',result.get('ok',False)) is not True:
                raise RuntimeError('Native CLI outcome is unconfirmed; automatic redispatch refused')
            self.store.settle(job,result)
            self.store.save(job,process=None)
            return result
        except BaseException:
            self.store.save(job,'STOPPED',reason='Model outcome unconfirmed; candidate retained')
            raise

    def maintain(self,job):
        self.check_cancelled(job)
        config,name,provider,settings=self.configured(job)
        if job['no_progress']>=settings.get('no_progress_limit',2):
            raise RuntimeError('Maintenance stopped after successive corrections without verified improvement')
        if not job.get('candidate'):
            suffix='-'+str(job['episode']) if job.get('episode') else ''
            snapshot=self.directory/('project'+suffix)
            request=[sys.executable,'-m','auto_agents']
            env={**os.environ,'PYTHONPATH':str(Path(job['engine'])/'src'),'AUTO_AGENTS_NO_SUPERVISOR':'1'}
            proc=subprocess.run([*request,'snapshot','--project',job['project'],'--output',str(snapshot)],
                                env=env,capture_output=True,text=True)
            if proc.returncode: raise RuntimeError('Business snapshot failed: '+proc.stderr)
            exported=json.loads(proc.stdout)
            if exported['state']['pending_external']: raise RuntimeError('External outcomes require reconciliation')
            candidate=self.directory/('candidate'+suffix)
            binding=delivery.prepare(job['engine'],candidate)
            self.store.save(job,candidate=str(candidate),snapshot=str(snapshot),binding=binding,
                            provider=name,base=binding['base'])
        sandbox=self.sandbox_factory(self.store.root/'sandboxes'/job['id'],provider=provider,engine=job['engine'])
        if settings.get('max_duration_seconds'):
            sandbox.deadline=job.get('maintenance_started',job['created'])+settings['max_duration_seconds']
        sandbox.prepare()
        driver=self.driver_factory(provider,sandbox,config.get('efforts',{}))
        verification_root=self.directory/('verification-'+str(job['episode']) if job.get('episode') else 'verification')
        verifier=self.verifier_factory(job['engine'],job['base'],verification_root,sandbox)
        token=job['resume_token']
        if not job.get('original_reproduction_confirmed'):
            reproduced=verifier.recover(Path(job['candidate']),Path(job['snapshot']),token)
            self.store.save(job,original_reproduction=reproduced)
            if reproduced['status']=='passed':
                running=job.get('observation',{}).get('runtime_revision')
                if not running or running==job['base']:
                    raise RuntimeError('Original defect cannot be reproduced; automatic editing refused')
                self.store.save(job,needs_verification=True,existing_source_clears_fault=True)
            else:
                report=reproduced.get('report') or {}
                if (report.get('category')!='engine' or report.get('type')!=job['fault']['type']):
                    raise RuntimeError('Offline check did not reproduce the original engine exception; evidence retained')
                cycle_fault=job['fault'].get('evidence',{}).get('monitor')=='control_cycle'
                same=(report.get('reason')=='Repeated business step without verified progress' if cycle_fault
                      else normalized_error(report.get('reason'))==normalized_error(job['fault']['message']))
                visited={step['step_id'] for step in report.get('steps',[])}
                if not same or token['step_id'] not in visited:
                    raise RuntimeError('Offline failure differs from the original checkpoint; automatic editing refused')
            self.store.save(job,original_reproduction_confirmed=True)
        retained_check = bool(job.get('needs_verification') or job.get('candidate_revision') and not job.get('verification'))
        while True:
            if settings.get('max_duration_seconds') and time.time()-job.get('maintenance_started',job['created']) >= settings['max_duration_seconds']:
                raise RuntimeError('Configured maintenance duration limit reached')
            prompt=('Repair only the engine defect blocking this original task. Reproduce it before editing. '
                'Preserve the original goal, authorization and business state. Do not edit existing tests, '
                'maintenance source, dependencies, or data formats. Add regression tests under tests/repair/. '
                'The candidate and latest failure are retained; do not replan unrelated work.\n'
                +json.dumps({'fault':job['fault'],'checkpoint':token,'verification':job.get('verification')},ensure_ascii=False))
            if not retained_check and not job.get('verification',{}).get('ok'):
                self.store.save(job,'REPAIRING')
                result=self.model(job,driver,'implement',prompt)
                if not result['ok']: raise RuntimeError(result.get('reason') or 'Repair CLI failed')
            retained_check = False
            if job.get('needs_verification'):self.store.save(job,needs_verification=False)
            # Protect inputs before staging candidate changes.
            scope=verifier.scope(Path(job['candidate']))
            if not scope['ok']: raise RuntimeError('Candidate changed protected paths: '+', '.join(scope['paths']))
            self.check_cancelled(job)
            revision=delivery.commit(job['candidate'],'fix: engine maintenance '+job['id'])
            self.store.save(job,'VERIFYING',candidate_revision=revision)
            verification=verifier.verify(Path(job['candidate']),Path(job['snapshot']),token)
            if verification['ok']:
                review=self.model(job,driver,'review',review_prompt(job,verification))
                try: verdict=json.loads(review['text'])
                except ValueError: raise RuntimeError('Independent review returned an invalid verdict')
                verification['checks'].append({'id':'independent-review','status':'passed' if review['ok'] and verdict.get('ok') is True else 'failed'})
                verification['ok']=review['ok'] and verdict.get('ok') is True
            self.store.save(job,verification=verification)
            if verification['ok']: break
            _,allowed=self.store.progress(job,verification['checks'],settings.get('no_progress_limit',2))
            if not allowed: raise RuntimeError('Two successive corrections have no new verified improvement')
        self.check_cancelled(job)
        self.store.save(job,'RESTARTING')
        delivery.deliver(job['binding'],job['candidate'])
        runtime=self.install(job)
        self.store.save(job,runtime_argv=runtime,needs_maintenance=False)
        try:
            # Publication uses a separate copy; conflicts cannot damage accepted code.
            source=Path(job['engine']); branch=job['binding']['branch']
            remote=delivery.git(source,'config','--get','branch.'+branch+'.remote',check=False).stdout.strip()
            ref=delivery.git(source,'config','--get','branch.'+branch+'.merge',check=False).stdout.strip()
            if config.get('execution',{}).get('supervision',{}).get('publish',True) and remote and ref:
                url=delivery.git(source,'remote','get-url',remote)
                publication=self.directory/('publication-'+str(job['episode']) if job.get('episode') else 'publication')
                if not publication.exists(): subprocess.run(['git','clone','--no-local',job['candidate'],str(publication)],check=True,capture_output=True)
                def revalidate():
                    report=verifier.verify(publication,Path(job['snapshot']),token,
                        scope_base=delivery.git(publication,'rev-parse','FETCH_HEAD'))
                    if report['ok']:
                        reviewed=self.model(job,driver,'review',review_prompt(job,report),candidate=publication)
                        try: report['ok']=reviewed['ok'] and json.loads(reviewed['text']).get('ok') is True
                        except ValueError: report['ok']=False
                    return report
                result=delivery.publish(publication,url,ref,revalidate)
                self.store.save(job,publication=result)
        except Exception as error:
            self.store.save(job,publication={'state':'pending','reason':str(error)})
        return True

    def install(self,job):
        revision=job['candidate_revision']
        installation=Path(os.environ.get('XDG_STATE_HOME',str(Path.home()/'.local/state')))/'auto-agents/installations'/digest(job['engine'])[:24]
        version=installation/'versions'/revision
        source=version/'source'; python=version/'venv/bin/python'
        if not source.exists():
            source.parent.mkdir(parents=True,exist_ok=True)
            subprocess.run(['git','clone','--no-local',job['candidate'],str(source)],check=True,capture_output=True)
            delivery.git(source,'checkout','--detach',revision)
        ready=version/'ready.json'
        if not ready.exists():
            if not python.exists(): subprocess.run([sys.executable,'-m','venv',str(version/'venv')],check=True,capture_output=True)
            with (version/'installation.log').open('w') as output:
                subprocess.run([str(python),'-m','pip','install',str(source)],check=True,stdout=output,stderr=subprocess.STDOUT)
                subprocess.run([str(python),'-c','import auto_agents.bootstrap, auto_agents.cli_impl'],
                    check=True,env={**os.environ,'PYTHONPATH':str(source/'src')},stdout=output,stderr=subprocess.STDOUT)
            atomic(ready,{'revision':revision,'python':str(python),'source':str(source)})
        self.store.save(job,runtime_source=str(source))
        pointer=installation/'current.json'
        if pointer.exists(): shutil.copy2(pointer,installation/'previous.json')
        atomic(pointer,{'schema':1,'source':str(source),'python':str(python),'revision':revision})
        argv=list(job['argv'])
        if '-m' in argv:
            argv[0]=str(python)
        else:
            argv=[str(python),'-m','auto_agents',*argv[1:]]
        return argv

    def resume(self,identity, *, explicit=False):
        job=self.store.get(identity); self.directory=self.store.root/'jobs'/identity
        if job['state']=='DONE':
            with self.locked(job):self.clean_completed(job)
            self.clean_business(job)
            return self.store.get(identity)
        if job.get('cancel_requested') and not explicit:
            return job
        if job.get('active_call'):
            raise RuntimeError('Unconfirmed model operation retained; automatic redispatch refused')
        if alive(job.get('process')):
            raise RuntimeError('Original process is still alive; duplicate execution refused')
        try:
            with self.locked(job):
                if explicit:self.store.acknowledge_resume(job)
                if (job.get('needs_maintenance') or job.get('fault') and not job.get('runtime_argv')) and job['state'] in {'STOPPED','REPAIRING','VERIFYING','RESTARTING'}:
                    self.maintain(job)
                while self.business(job):
                    self.maintain(job)
                if job['state']=='DONE':self.clean_completed(job)
        except Exception as error:
            self.store.save(job,'STOPPED',reason=str(error))
        if job['state']=='DONE':self.clean_business(job)
        return self.store.get(identity)

    def clean_completed(self,job):
        from .cleanup import completed_job
        try:result=completed_job(self.store,job)
        except (OSError,RuntimeError,ValueError,subprocess.SubprocessError) as error:
            result={'state':'pending','reason':str(error)}
        self.store.save(job,cleanup=result)

    def clean_business(self,job):
        """Request bounded project maintenance after releasing its run lock."""
        if os.environ.get('AUTO_AGENTS_STORAGE_MAINTENANCE')=='off':return
        argv=job.get('runtime_argv') or job['argv']
        if 'auto_agents' in argv and '-m' in argv:
            prefix=argv[:argv.index('auto_agents')+1]
        elif len(argv)>1 and Path(argv[1]).name=='auto_agents.py':prefix=argv[:2]
        elif Path(argv[0]).name in {'auto-agents','auto-agents.exe'}:prefix=argv[:1]
        else:return
        environment=self.env(job)
        for key in ('AUTO_AGENTS_RUN_LOCK_FD','AUTO_AGENTS_RUN_TOKEN','AUTO_AGENTS_RUN_LOCK_KEY','AUTO_AGENTS_OBSERVATION_FILE'):
            environment.pop(key,None)
        try:
            result=subprocess.run([*prefix,'storage','maintain','--project',job['project']],
                cwd=job.get('cwd',job['engine']),env=environment,capture_output=True,text=True,timeout=45)
            report=json.loads(result.stdout)
        except (OSError,ValueError,subprocess.SubprocessError) as error:
            report={'ok':False,'reason':str(error)}
        self.store.save(job,business_cleanup=report)


def retry_publication(store, identity):
    job=store.get(identity)
    if isinstance(job.get('publication'),dict) and job['publication'].get('state')=='published':return job
    if job.get('cleanup',{}).get('state')=='released':
        raise RuntimeError('Completed task inputs were released; publish the accepted revision with normal Git')
    if job.get('active_call') or alive(job.get('process')):
        raise RuntimeError('Publication requires a quiescent job with confirmed model outcomes')
    if job.get('cancel_requested'):
        raise RuntimeError('Cancelled maintenance requires explicit resume before publication')
    if not job.get('candidate_revision') or not job.get('verification',{}).get('ok'):
        raise RuntimeError('Publication requires an accepted local revision')
    runner=Runner(store);runner.directory=store.root/'jobs'/identity
    config,_,provider,_=runner.configured(job)
    with runner.locked(job):
        runner.check_cancelled(job)
        sandbox=runner.sandbox_factory(store.root/'sandboxes'/job['id'],provider=provider,engine=job['engine']);sandbox.prepare()
        driver=runner.driver_factory(provider,sandbox,config.get('efforts',{}))
        verification_root=runner.directory/('verification-'+str(job['episode']) if job.get('episode') else 'verification')
        verifier=runner.verifier_factory(job['engine'],job['base'],verification_root,sandbox)
        source=Path(job['engine']);branch=job['binding']['branch']
        remote=delivery.git(source,'config','--get','branch.'+branch+'.remote')
        ref=delivery.git(source,'config','--get','branch.'+branch+'.merge')
        url=delivery.git(source,'remote','get-url',remote)
        publication=runner.directory/'publication-retry'
        if publication.exists():shutil.rmtree(publication)
        subprocess.run(['git','clone','--no-local',job['candidate'],str(publication)],check=True,capture_output=True)
        def validate():
            report=verifier.verify(publication,Path(job['snapshot']),job['resume_token'],
                scope_base=delivery.git(publication,'rev-parse','FETCH_HEAD'))
            if report['ok']:
                review=runner.model(job,driver,'review',review_prompt(job,report),candidate=publication)
                try:report['ok']=review['ok'] and json.loads(review['text']).get('ok') is True
                except ValueError:report['ok']=False
            return report
        result=delivery.publish(publication,url,ref,validate)
        store.save(job,publication=result)
    return store.get(identity)
