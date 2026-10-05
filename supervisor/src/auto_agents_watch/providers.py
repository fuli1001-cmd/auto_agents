"""Thin native CLI adapters; configuration is shared, execution code is not."""
from pathlib import Path
import json
import os
import shutil

from .process import run

SUPPORTED = {'codex','claude-code','copilot-cli'}


def copilot_profile(config, effort):
    extra=list(config.get('extra_args',[]))
    for index,arg in enumerate(extra):
        if arg=='--config-dir' and index+1<len(extra):
            return Path(extra[index+1]).expanduser().resolve()
        if arg.startswith('--config-dir='):
            return Path(arg.split('=',1)[1]).expanduser().resolve()
    profile=config.get('profile_map',{}).get(effort)
    if not profile:return None
    path=Path(profile).expanduser()
    if path.is_absolute():return path
    env=environment(config)
    home=Path(env.get('COPILOT_HOME') or Path(env.get('HOME',str(Path.home())))/'.copilot')
    return home/'profiles'/profile


def selection(config, argv):
    name = None
    for i,arg in enumerate(argv):
        if arg == '--provider' and i + 1 < len(argv): name = argv[i+1]
        elif arg.startswith('--provider='): name = arg.split('=',1)[1]
    name = name or config.get('active_provider')
    value = (config.get('providers') or {}).get(name)
    if not isinstance(value, dict): raise ValueError('Selected provider is not configured: ' + str(name))
    if value.get('kind','codex') not in SUPPORTED:
        raise ValueError('Selected provider has no maintenance CLI adapter: ' + str(name))
    return name, dict(value)


def arguments(config, role, effort):
    kind = config.get('kind','codex')
    binary = config.get('binary') or ('claude' if kind == 'claude-code' else 'codex' if kind == 'codex' else 'copilot' if kind=='copilot-cli' else 'antigravity')
    profile = config.get('profile_map',{}).get(effort, effort)
    extra = list(config.get('extra_args', []))
    if kind == 'codex':
        # Docker supplies the writer/reviewer filesystem boundary. Starting a
        # second OS sandbox inside it would recreate the retired nesting.
        return [binary,'exec','--json','--dangerously-bypass-approvals-and-sandbox',
                '-p',profile,*extra,'-']
    if kind == 'claude-code':
        return [binary,'-p','--output-format','stream-json','--verbose','--model',profile,
                *(['--permission-mode','dontAsk'] if role=='review' else ['--dangerously-skip-permissions']),*extra]
    if kind == 'copilot-cli':
        directory=copilot_profile(config,effort)
        mapped=[];index=0
        while index<len(extra):
            if extra[index]=='--config-dir':index+=2;continue
            if extra[index].startswith('--config-dir='):index+=1;continue
            mapped.append(extra[index]);index+=1
        command=[binary,'--output-format','json','--stream','on','--allow-all','--no-ask-user']
        if directory:
            if not directory.is_dir():raise ValueError('Selected Copilot profile is unavailable: '+str(directory))
            command+=['--config-dir','/home/agent/.copilot/selected-profile']
            settings={}
            for name in ('config.json','settings.json'):
                path=directory/name
                if path.is_file():settings.update(json.loads(path.read_text()))
            if settings.get('model') and not any(arg in {'--model','-m'} or arg.startswith('--model=') for arg in mapped):
                command+=['--model',str(settings['model'])]
        return [*command,*mapped,'-p']
    return [binary,'--model',profile,'--output-format','stream-json',*extra,'--prompt']


def environment(config, base=None):
    env = dict(base or os.environ)
    for key,value in config.get('environment',{}).items():
        if value is None: env.pop(key,None)
        else: env[key] = str(value)
    return env


def final_message(log, kind):
    text = Path(log).read_text(errors='replace')
    messages, failed, completed, terminal_failure = [], False, False, False
    for line in text.splitlines():
        try: event = json.loads(line)
        except ValueError: continue
        if not isinstance(event,dict): continue
        name = event.get('type') or event.get('event') or ''
        if name in {'turn.failed','error','session.error','assistant.error'} or event.get('is_error'):
            failed = True
        if name in {'turn.failed','session.error'} or name=='result' and event.get('is_error'):
            terminal_failure = True
        if kind == 'codex' and name == 'turn.completed': completed = True
        if kind == 'claude-code' and name == 'result' and event.get('subtype','success') == 'success': completed = True
        if kind == 'copilot-cli' and name in {'session.end','assistant.turn_end'}: completed = True
        item = event.get('item') or {}
        if item.get('type') == 'agent_message' and isinstance(item.get('text'),str): messages.append(item['text'])
        if name == 'result' and isinstance(event.get('result'),str): messages.append(event['result'])
        if name == 'assistant':
            for block in event.get('message',{}).get('content',[]):
                if block.get('type') == 'text': messages.append(block.get('text',''))
        if name in {'assistant.message','assistant.turn_end'}:
            data = event.get('data') or {}
            if isinstance(data,dict) and isinstance(data.get('content'),str): messages.append(data['content'])
    return {'ok':bool(messages) and completed and not failed,'text':messages[-1] if messages else '',
            'confirmed':completed or terminal_failure,
            'reason':'Native CLI has no confirmed final result' if not messages or not completed or failed else ''}


class Driver:
    def __init__(self, config, sandbox, efforts):
        self.config, self.sandbox, self.efforts = config, sandbox, efforts

    def call(self, role, candidate, evidence, prompt, log, *, on_start=None, cancelled=None, timeout=None):
        effort = self.efforts.get('self_repair_review' if role=='review' else 'self_repair',
                                  'max' if role=='review' else 'deep')
        argv = arguments(self.config, role, effort)
        kind = self.config.get('kind','codex')
        env = environment(self.config)
        # Prompt switches for these CLIs take their prompt as an argument.
        stdin = prompt if kind in {'codex','claude-code'} else None
        if stdin is None: argv.append(prompt)
        result = self.sandbox.agent(argv, candidate, evidence, env, log, readonly=role=='review',
                                    stdin=stdin, on_start=on_start,cancelled=cancelled,
                                    profile=copilot_profile(self.config,effort) if kind=='copilot-cli' else None,
                                    timeout=timeout or self.config.get('timeout_seconds'))
        parsed = final_message(log,kind)
        return {**result, **parsed, 'ok':result['ok'] and parsed['ok'],
                'confirmed':parsed['confirmed'] and not result.get('reason')}
