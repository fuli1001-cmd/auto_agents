"""Maintenance commands do not import business implementation modules."""
import argparse
import json
import sys
from pathlib import Path
import shlex

from .store import Store
from .runner import Runner


def readable_result(value, store):
    if 'jobs' in value:
        if not value['jobs']:
            print('没有监督任务。')
        for job in value['jobs']:
            print(f"{job['id']}  {job['state']}  {job.get('project', '')}")
        return
    labels = {'DONE': '已完成', 'STOPPED': '已停止', 'RUNNING': '运行中',
              'REPAIRING': '修复中', 'VERIFYING': '验证中', 'RESTARTING': '恢复中'}
    print('监督任务' + labels.get(value.get('state'), value.get('state', '已结束')) + '。')
    reason = value.get('reason', '')
    if reason:
        explanations = {
            'Engine source has uncommitted changes; candidate admission preserves user work':
                '引擎仓库有未提交改动，自动修复暂时无法启动；现有改动和会话已保留。',
            'Original business task completed': '原任务已完成。',
            'Business task stopped without an engine defect': '业务流程已停止，未检测到引擎异常。',
            'Execution health is uncertain; checkpoint retained': '无法确认运行状态，已保存恢复检查点。',
        }
        summary = ' '.join(str(explanations.get(reason, reason)).split())
        print('原因：' + (summary if len(summary) <= 400 else summary[:397] + '…'))
    fault = value.get('fault') or value.get('subsequent_fault') or {}
    if fault.get('message') and fault['message'] != reason:
        evidence = fault.get('evidence') or {}
        seed = evidence.get('issue_seed') or evidence.get('spec_seed') or {}
        message = seed.get('user_summary') or fault['message']
        if message == 'Repeated execution without progress':
            message = '同一操作反复执行，未产生已验证的进展。'
        message = ' '.join(str(message).split())
        print('问题：' + (message if len(message) <= 400 else message[:397] + '…'))
    if value.get('id'):
        directory = store.root / 'jobs' / value['id']
        print('日志：' + str(directory / 'business.log'))
        print('查看完整诊断：auto-agents-watch status --root ' + shlex.quote(str(store.root))
              + ' --job ' + shlex.quote(value['id']) + ' --json')


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
        if args.json:
            print(json.dumps(value,ensure_ascii=False,indent=2))
        else:
            readable_result(value, store)
        return 0 if value.get('state') in {'DONE','RUNNING','RESTARTING'} or args.action in {'status','cancel','reconcile'} else 3
    except (OSError,RuntimeError,ValueError) as error:
        if args.json:
            print(json.dumps({'ok':False,'reason':str(error)},ensure_ascii=False),file=sys.stderr)
        else:
            reason = ' '.join(str(error).split())
            print('监督任务已停止：' + (reason if len(reason) <= 400 else reason[:397] + '…'), file=sys.stderr)
        return 3
