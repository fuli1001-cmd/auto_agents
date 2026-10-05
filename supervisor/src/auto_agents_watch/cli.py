"""Maintenance commands do not import business implementation modules."""
import argparse
import json
import sys

from .store import Store
from .runner import Runner


def main(argv=None):
    argv=list(sys.argv[1:] if argv is None else argv)
    separator=argv.index('--') if '--' in argv else len(argv)
    command=argv[separator+1:]
    parser=argparse.ArgumentParser(prog='auto-agents-watch')
    parser.add_argument('action',choices=['run','status','resume','cancel','retry-publish','reconcile'])
    parser.add_argument('--engine')
    parser.add_argument('--job')
    parser.add_argument('--root')
    parser.add_argument('--json',action='store_true')
    parser.add_argument('--result')
    args=parser.parse_args(argv[:separator])
    try:
        store=Store(args.root)
        if args.action=='run':
            if not args.engine or not command: parser.error('run requires --engine and -- COMMAND')
            value=Runner(store).start(command,args.engine)
        elif args.action=='status': value=store.get(args.job) if args.job else {'jobs':store.list()}
        elif args.action=='cancel':
            if not args.job: parser.error('--job required')
            value=store.get(args.job); store.save(value,cancel_requested=True)
            from .process import cancel_owned
            cancel_owned(value.get('process'))
        elif args.action=='resume':
            if not args.job: parser.error('--job required')
            value=Runner(store).resume(args.job,explicit=True)
        elif args.action=='reconcile':
            if not args.job or not args.result:parser.error('reconcile requires --job and --result')
            from .process import alive
            from pathlib import Path
            value=store.get(args.job)
            if not value.get('active_call') or alive(value.get('process')):
                raise ValueError('Only a quiescent unconfirmed model call can be reconciled')
            receipt=json.loads(Path(args.result).read_text())
            if receipt.get('job_id')!=args.job or receipt.get('call')!=value['active_call']['number']:
                raise ValueError('Confirmed result belongs to another maintenance call')
            result=receipt.get('result') or {}
            if result.get('confirmed') is not True or type(result.get('ok')) is not bool:
                raise ValueError('Operator must confirm the original native result or cancellation')
            runner=Runner(store)
            with runner.locked(value):
                from .process import cancel_owned
                cancel_owned(value.get('process'))
                writer=value['active_call']['role']=='implement'
                store.settle(value,result)
                store.save(value,'STOPPED',needs_verification=writer,process=None,
                           reason='Original call reconciled; explicit resume required')
        else:
            if not args.job: parser.error('--job required')
            from .runner import retry_publication
            value=retry_publication(store,args.job)
        print(json.dumps(value,ensure_ascii=False,indent=2))
        return 0 if value.get('state') in {'DONE','RUNNING','RESTARTING'} or args.action in {'status','cancel','reconcile'} else 3
    except (OSError,RuntimeError,ValueError) as error:
        print(json.dumps({'ok':False,'reason':str(error)},ensure_ascii=False),file=sys.stderr)
        return 3
