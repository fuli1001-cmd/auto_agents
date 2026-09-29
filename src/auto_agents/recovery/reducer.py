"""Pure transitions. Executors cannot reopen incidents or invent retry credit."""
from copy import deepcopy
import json

from .model import Command, Contract, Evidence, Outcome, OutcomeKind, canonical, digest, identifier, require


def initial():
    return {'schema': 1, 'revision': 0, 'workflow_id': '', 'project': '', 'goal_id': '',
            'status': 'active', 'tasks': {}, 'incidents': {}, 'continuations': {},
            'adoptions': {}, 'commands': {}, 'operations': {}, 'projections': {},
            'budget': {'model_calls': 0, 'implementations': 0, 'limit': None,
                       'repair_model_calls':0,'repair_implementations':0,'repair_transactions':0,
                       'repair_limits':{'model_calls':None,'implementations':None,'transactions':None},
                       'stagnant': 0, 'rediagnoses': 0, 'diagnosis_due': False, 'progress': []}, 'imports': {}}


def _task(state, key):
    require(key in state['tasks'], 'task', 'Unknown task', task_id=key)
    return state['tasks'][key]


def _command(state, key):
    require(key in state['commands'], 'command', 'Unknown command')
    return state['commands'][key]


def _proof(task, command, evidence):
    require(evidence.task_id == command['task_id'] and evidence.source == command['source']
            and evidence.contract == command['contract'] and evidence.environment == command['environment']
            and evidence.phase == command['phase'], 'evidence_binding', 'Phase proof belongs to different inputs')


def _projection(state, data):
    require(data.get('name') and data.get('blob'), 'projection', 'Projection needs an immutable blob')
    prior = state['projections'].get(data['name'])
    require(prior is None or data.get('previous') == prior['blob'], 'stale_projection', 'Stale domain writer')
    if prior:
        require(all(data.get('counters', {}).get(k, v) >= v for k, v in prior.get('counters', {}).items()),
                'budget_regression', 'Resume cannot decrease recorded consumption')
        require(prior.get('terminal') is not True or data.get('terminal') is True,
                'terminal_projection', 'A completed domain projection cannot be reopened')
    state['projections'][data['name']] = data


def decide(snapshot, event):
    """Return a new snapshot and an outbox; never perform any side effect."""
    state = deepcopy(snapshot) if snapshot else initial()
    data, kind, outbox = dict(event.data), event.kind, []
    if kind == 'workflow_registered':
        identifier(data['workflow_id']); identifier(data['goal_id'])
        require(not state['workflow_id'], 'workflow', 'Workflow already exists')
        require(bool(data['project']), 'workflow', 'Project identity is required')
        state.update(workflow_id=data['workflow_id'], goal_id=data['goal_id'], project=data['project'])
        limit = data.get('model_call_limit')
        require(limit is None or type(limit) is int and limit > 0, 'budget', 'Invalid explicit call limit')
        state['budget']['limit'] = limit
    elif kind == 'task_bound':
        contract = Contract.read(data['contract'])
        require(contract.goal_id == state['goal_id'], 'task', 'Task belongs to another goal')
        if contract.parent_task: _task(state, contract.parent_task)
        existing = state['tasks'].get(contract.task_id)
        require(existing is None or existing['contract_id'] == contract.identity, 'contract_changed',
                'A bound task contract cannot be overwritten')
        if existing is None:
            state['tasks'][contract.task_id] = {'contract': contract.to_dict(), 'contract_id': contract.identity,
                'phase': contract.phases[0], 'status': 'ready', 'attempts': 0, 'candidate': None,
                'active_command': None, 'proofs': {}, 'failure': None}
    elif kind == 'candidate_retained':
        task = _task(state, data['task_id'])
        require(task['status'] != 'completed', 'terminal_task', 'Completed task cannot acquire a new candidate')
        require(all(data.get(k) for k in ('candidate_id', 'source', 'base', 'receipt')),
                'candidate', 'Candidate custody must be complete')
        old = task['candidate']
        require(old is None or old['candidate_id'] == data['candidate_id'] or data.get('parent') == old['candidate_id'],
                'candidate_lineage', 'Candidate replacement must preserve its parent')
        task['candidate'] = {k: data.get(k) for k in ('candidate_id', 'source', 'base', 'receipt', 'parent')}
    elif kind == 'task_restored':
        task = _task(state, data['task_id'])
        require(data['manifest'] in state['imports'] and task['active_command'] is None,
                'migration', 'Task restoration requires its imported manifest')
        require(data['phase'] in task['contract']['phases'] and data['status'] in {'ready', 'blocked', 'completed'},
                'migration', 'Invalid imported task state')
        require(type(data['attempts']) is int and data['attempts'] >= task['attempts'],
                'migration', 'Historical attempts cannot be erased')
        task.update(phase=data['phase'], status=data['status'], attempts=data['attempts'],
                    failure=data.get('failure'), legacy_ref=data['legacy_ref'])
    elif kind == 'incident_restored':
        require(data['manifest'] in state['imports'] and data.get('record_ref'), 'migration', 'Unsealed incident import')
        require(data['incident_id'] not in state['incidents'] and data['status'] in {'open', 'resolved'},
                'migration', 'Invalid incident restoration')
        _task(state, data['task_id'])
        require(data['status'] != 'resolved' or data.get('resolution_ref'), 'migration', 'Closed incident needs retained recovery evidence')
        state['incidents'][data['incident_id']] = dict(data)
    elif kind == 'incident_alias_imported':
        require(data['manifest'] in state['imports'], 'migration', 'Incident alias needs sealed provenance')
        incident = state['incidents'].get(data['incident_id'])
        require(incident is not None, 'migration', 'Alias has no incident')
        incident.setdefault('legacy_jobs', []).append(data['legacy_job'])
        if data.get('resolution_ref'):
            incident.update(status='resolved', resolution_ref=data['resolution_ref'],
                            record_ref=data['record_ref'], runtime=data.get('runtime'))
    elif kind == 'command_reserved':
        command = Command(**data)
        task = _task(state, command.task_id)
        require(command.workflow_id == state['workflow_id'] and command.contract == task['contract_id'],
                'command_binding', 'Command belongs to another workflow or contract')
        require(state['status'] == 'active' and task['status'] == 'ready', 'dispatch_blocked', 'Task cannot dispatch')
        require(command.phase == task['phase'], 'phase', 'Command is not the authorized next stage')
        previous = state['operations'].get(command.operation_key)
        require(previous is None or previous == command.command_id, 'duplicate_operation',
                'Operation already has a durable command identity', command_id=previous)
        require(command.command_id not in state['commands'] and task['active_command'] is None,
                'command_active', 'A command is already active')
        budget = state['budget']
        if command.model_call:
            if 'recovery' in state:
                from .convergence import consume
                consume(state, command)
            else:
                require(not budget['diagnosis_due'] or command.phase == 'diagnose', 'diagnosis_required', 'One bounded diagnosis is required before further implementation')
                from .model_progress import require_progress
                require_progress(state, command.phase)
            require(budget['limit'] is None or budget['model_calls'] < budget['limit'], 'budget_exhausted', 'Goal call limit reached')
            budget['model_calls'] += 1
            if task['contract']['kind'] == 'engine_repair' or command.phase == 'review':
                limit = budget['repair_limits']['model_calls']
                require(limit is None or budget['repair_model_calls'] < limit,
                        'budget_exhausted','Explicit repair call limit reached')
                budget['repair_model_calls'] += 1
        if command.phase == 'implement':
            if task['contract']['kind'] == 'engine_repair':
                limit = budget['repair_limits']['implementations']
                require(limit is None or budget['repair_implementations'] < limit,
                        'budget_exhausted','Explicit repair implementation limit reached')
                budget['repair_implementations'] += 1
            budget['implementations'] += 1
            task['attempts'] += 1
        state['commands'][command.command_id] = {**command.to_dict(), 'status': 'reserved', 'epoch': 0, 'owner': '', 'outcome': None,
                                                'sequence':state['revision'] + 1}
        state['operations'][command.operation_key] = command.command_id
        task.update(status='running', active_command=command.command_id)
        outbox.append(command.to_dict())
    elif kind == 'command_dispatched':
        command = _command(state, data['command_id'])
        require(command['status'] == 'reserved', 'dispatch_duplicate', 'Command cannot be dispatched twice')
        require(type(data['epoch']) is int and data['epoch'] > command['epoch'] and bool(data['owner']),
                'lease', 'Dispatch needs a current fenced lease')
        command.update(status='running', epoch=data['epoch'], owner=data['owner'])
    elif kind == 'command_finished':
        command = _command(state, data['command_id']); task = _task(state, command['task_id'])
        require(command['status'] in {'running', 'unknown'}, 'receipt_duplicate', 'Command is already settled')
        require(command['epoch'] == data['epoch'] and command['owner'] == data['owner'], 'stale_owner', 'Expired worker cannot commit')
        outcome = Outcome.read(data['outcome'])
        for proof in outcome.evidence: _proof(task, command, proof)
        command['outcome'] = outcome.to_dict()
        if outcome.kind == OutcomeKind.OUTCOME_UNKNOWN:
            command['status'] = 'unknown'
            task.update(status='blocked', failure=outcome.to_dict())
        else:
            command['status'] = 'finished'
            task['active_command'] = None
            task['failure'] = None if outcome.kind == OutcomeKind.SUCCESS else outcome.to_dict()
            if outcome.kind == OutcomeKind.SUCCESS:
                proofs = [p.to_dict() for p in outcome.evidence]
                task['proofs'][command['phase']] = proofs
                budget = state['budget']
                if command['phase'] == 'diagnose' and 'recovery' not in state:
                    require(budget['diagnosis_due'] and budget['rediagnoses'] == 0, 'diagnosis', 'No diagnosis credit remains')
                    budget.update(stagnant=0, rediagnoses=1, diagnosis_due=False)
                for proof in outcome.evidence:
                    key = digest([task['contract']['goal_id'], task['contract']['required_checks'], proof.predicate])
                    if 'recovery' not in state and key not in budget['progress'] and proof.phase in {'verify', 'deliver', 'acceptance'}:
                        budget['progress'].append(key)
                        budget.update(stagnant=0, rediagnoses=0, diagnosis_due=False)
                phases = task['contract']['phases']; index = phases.index(command['phase'])
                if index + 1 < len(phases): task.update(phase=phases[index + 1], status='ready')
                else:
                    require(any(p.predicate == task['contract']['completion'] for p in outcome.evidence),
                            'completion_proof', 'Final stage does not prove its declared completion boundary')
                    task['status'] = 'completed'
            elif outcome.kind == OutcomeKind.CANDIDATE_REJECTED:
                budget = state['budget']
                if 'recovery' in state:
                    task.update(status='ready' if 'implement' in task['contract']['phases'] else 'blocked',
                                phase='implement' if 'implement' in task['contract']['phases'] else command['phase'])
                else:
                    budget['stagnant'] += 1
                if 'recovery' not in state and budget['stagnant'] >= 2:
                    retry_phase = 'implement' if 'implement' in task['contract']['phases'] else command['phase']
                    if budget['rediagnoses'] >= 1: task.update(status='blocked', phase=retry_phase)
                    else:
                        budget['diagnosis_due'] = True
                        task.update(status='blocked', phase=retry_phase)
                        task['failure']['details'] = {**task['failure']['details'], 'next_action': 'bounded_rediagnosis'}
                elif 'recovery' not in state: task.update(status='ready' if 'implement' in task['contract']['phases'] else 'blocked',
                                  phase='implement' if 'implement' in task['contract']['phases'] else command['phase'])
            else: task['status'] = 'blocked'
        if 'recovery' in state and outcome.kind != OutcomeKind.OUTCOME_UNKNOWN:
            from .convergence import observe, record_diagnosis
            if (command['phase'] == 'verify' or command['phase'] == 'review'
                    and outcome.kind == OutcomeKind.CANDIDATE_REJECTED) and outcome.details.get('verification_observation'):
                observe(state, command, outcome.details['verification_observation'])
            if command['phase'] == 'diagnose' and outcome.kind == OutcomeKind.SUCCESS:
                record_diagnosis(state, command, outcome.details.get('recovery_diagnosis'))
        parent_id = task['contract'].get('parent_task')
        if parent_id:
            parent = _task(state, parent_id)
            require(parent['status'] != 'completed', 'terminal_task', 'A phase cannot reopen its completed parent')
            if outcome.kind == OutcomeKind.SUCCESS:
                parent['failure'] = None
                parent['proofs'][command['phase']] = [p.to_dict() for p in outcome.evidence]
                phases = parent['contract']['phases']
                if command['phase'] in phases:
                    index = phases.index(command['phase'])
                    if index + 1 < len(phases): parent.update(phase=phases[index + 1], status='ready')
                    elif any(p.predicate == parent['contract']['completion'] for p in outcome.evidence):
                        parent['status'] = 'completed'
            else:
                parent.update(failure=outcome.to_dict(), status=task['status'])
                if outcome.kind == OutcomeKind.CANDIDATE_REJECTED: parent['phase'] = 'implement'
    elif kind == 'native_prepared':
        task = _task(state, data['task_id'])
        require(task['status'] in {'ready','blocked'} and task['active_command'] is None
                and task['phase'] in {'prepare','clarify','route','requirements','architecture','plan'},
                'preparation', 'Only a prepared task may enter its next owned stage')
        require(data.get('binding_ref') and data['phase'] in task['contract']['phases'],
                'preparation', 'Prepared transition needs its retained binding')
        task.update(phase=data['phase'], status='ready', preparation=data['binding_ref'])
    elif kind == 'command_reconciled':
        command = _command(state, data['command_id'])
        require(command['status'] in {'running', 'unknown'}, 'reconciliation', 'No unresolved operation')
        require(data.get('receipt_ref') and type(data['epoch']) is int and data['epoch'] > command['epoch'],
                'reconciliation', 'Reconciliation requires durable evidence and a new lease')
        command.update(epoch=data['epoch'], owner=data['owner'])
        # Receipt validation/settlement still uses command_finished; no redispatch.
    elif kind == 'task_resumed':
        task = _task(state, data['task_id'])
        require(task['status'] == 'blocked' and task['active_command'] is None,
                'resume_blocked', 'Unknown effects must be reconciled before resume')
        require(data.get('evidence_ref'), 'resume_evidence', 'Resume needs new recovery evidence')
        failure = task.get('failure') or {}
        require(failure.get('kind') != OutcomeKind.CANDIDATE_REJECTED.value
                or 'recovery' in state or state['budget']['stagnant'] < 2,
                'no_progress', 'Unchanged stopped search cannot acquire another attempt')
        task['status'] = 'ready'
    elif kind == 'task_preparation_blocked':
        task = _task(state,data['task_id'])
        failure = Outcome.read(data['failure'])
        require(task['status'] != 'completed' and task['active_command'] is None and data.get('observation_ref')
                and failure.kind in {OutcomeKind.ENVIRONMENT_BLOCKED,OutcomeKind.NEED_INPUT,OutcomeKind.EVIDENCE_INVALID},
                'preparation','Preparation cannot settle an in-flight external effect')
        task.update(status='blocked',failure=failure.to_dict())
    elif kind == 'incident_opened':
        identifier(data['incident_id']); _task(state, data['task_id'])
        require(data['incident_id'] not in state['incidents'], 'incident_exists', 'Incident identity cannot be reused')
        outcome = Outcome.read(data['failure'])
        require(outcome.kind != OutcomeKind.SUCCESS and data.get('occurrence_ref'), 'incident', 'Failure observation is required')
        previous = data.get('previous')
        require(previous is None or previous in state['incidents'], 'incident', 'Unknown predecessor')
        if outcome.kind == OutcomeKind.ENGINE_DEFECT:
            budget = state['budget']; limit = budget['repair_limits']['transactions']
            require(limit is None or budget['repair_transactions'] < limit,
                    'budget_exhausted','A new incident cannot renew the explicit repair transaction budget')
            budget['repair_transactions'] += 1
        state['incidents'][data['incident_id']] = {**data, 'status': 'open', 'resolution': None}
    elif kind == 'incident_resolved':
        incident = state['incidents'].get(data['incident_id'])
        require(incident is not None and incident['status'] == 'open', 'incident_closed', 'Closed incidents cannot be reopened or resolved twice')
        proof = Evidence(**data['proof']); task = _task(state, incident['task_id'])
        require(proof.task_id == incident['task_id'] and proof.contract == task['contract_id'],
                'incident_proof', 'Recovery proof belongs to another task')
        require(data.get('continuation_id') and data.get('next_task_id'), 'continuation', 'Resolution must preserve a continuation')
        _task(state, data['next_task_id'])
        identifier(data['continuation_id'])
        require(data['continuation_id'] not in state['continuations'], 'continuation', 'Continuation already exists')
        incident.update(status='resolved', resolution=data['proof'], resolution_ref=proof.blob,
                        required_runtime=data.get('required_runtime'))
        state['continuations'][data['continuation_id']] = {'task_id': data['next_task_id'],
            'incident_id': data['incident_id'], 'proof': data['proof'], 'status': 'ready', 'operation': None}
        if data.get('publish'):
            outbox.append({'command_id': 'publish:' + data['incident_id'], 'phase': 'publish', 'payload': data['publish']})
    elif kind == 'continuation_consumed':
        item = state['continuations'].get(data['continuation_id'])
        require(item is not None and item['task_id'] == data['task_id'], 'continuation', 'Continuation owner mismatch')
        require(item['status'] == 'ready' or item['operation'] == data['operation'], 'continuation', 'Continuation already consumed')
        item.update(status='consumed', operation=data['operation'])
    elif kind == 'adoption_requested':
        identifier(data['adoption_id'])
        require(data['adoption_id'] not in state['adoptions'], 'adoption', 'Adoption already exists')
        require(all(data.get(k) for k in ('runtime', 'source', 'environment', 'contract')), 'adoption', 'Version adoption inputs are incomplete')
        state['adoptions'][data['adoption_id']] = {**data, 'status': 'requested', 'proofs': []}
    elif kind == 'adoption_verified':
        item = state['adoptions'].get(data['adoption_id'])
        require(item is not None and item['status'] == 'requested', 'adoption', 'Version is not awaiting verification')
        proofs = tuple(Evidence(**r) for r in data['proofs'])
        require(bool(proofs) and all(p.source == item['source'] and p.environment == item['environment']
                and p.contract == item['contract'] and p.phase == 'adopt' for p in proofs),
                'adoption_proof', 'Version proof does not match its exact inputs')
        item.update(status='verified', proofs=data['proofs'])
    elif kind == 'adoption_activated':
        item = state['adoptions'].get(data['adoption_id'])
        require(item is not None and item['status'] == 'verified', 'adoption', 'Only verified versions can be activated')
        require(not any(c['status'] in {'reserved', 'running', 'unknown'} for c in state['commands'].values()),
                'active_operations', 'Version switch requires a quiescent workflow')
        item['status'] = 'active'; state['runtime'] = item['runtime']
        # Incident terminal states and task candidates are deliberately untouched.
    elif kind == 'projection_saved':
        _projection(state,data)
    elif kind == 'projection_batch_saved':
        require(data.get('entries') and len({row['name'] for row in data['entries']}) == len(data['entries']),
                'projection','Atomic projection batch contains duplicate records')
        for row in data['entries']: _projection(state,row)
    elif kind == 'legacy_imported':
        require(data.get('manifest') and data.get('record'), 'migration', 'Sealed migration input is required')
        require(data['manifest'] not in state['imports'], 'migration', 'Migration was already applied')
        state['imports'][data['manifest']] = data['record']
        usage = data.get('usage', {})
        for key in ('model_calls', 'implementations','repair_model_calls','repair_implementations','repair_transactions'):
            amount = usage.get(key, 0)
            require(type(amount) is int and amount >= 0, 'migration', 'Invalid retained consumption')
            state['budget'][key] += amount
    elif kind == 'legacy_budget_bound':
        require(data['manifest'] in state['imports'],'migration','Budget import requires sealed provenance')
        budget = state['budget']
        for key, amount in data['usage'].items():
            require(key in {'repair_model_calls','repair_implementations','repair_transactions'} and type(amount) is int and amount >= 0,
                    'migration','Invalid historical repair consumption')
            difference = max(0, amount-budget[key])
            budget[key] += difference
            if key == 'repair_model_calls': budget['model_calls'] += difference
            if key == 'repair_implementations': budget['implementations'] += difference
        for key, limit in data['limits'].items():
            require(key in budget['repair_limits'] and (limit is None or type(limit) is int and limit > 0),
                    'budget','Invalid explicit repair policy')
            old = budget['repair_limits'][key]
            budget['repair_limits'][key] = old if limit is None else limit if old is None else min(old,limit)
    elif kind in {'recovery_policy_activated', 'recovery_permit_issued', 'recovery_observation_imported',
                  'recovery_auxiliary_reserved', 'recovery_auxiliary_finished'}:
        from .convergence import event as recovery_event
        recovery_event(state, kind, data)
    elif kind == 'workflow_stopped':
        require(data['status'] in {'paused', 'cancelled'}, 'workflow', 'Invalid stop status')
        state['status'] = data['status']
        if data['status'] == 'cancelled':
            for command in state['commands'].values():
                if command['status'] == 'reserved':
                    command.update(status='cancelled', outcome=Outcome(OutcomeKind.CANCELLED, 'Cancelled before dispatch').to_dict())
                    _task(state, command['task_id']).update(status='blocked', active_command=None, failure=command['outcome'])
    elif kind == 'workflow_resumed':
        require(state['status'] in {'paused', 'cancelled'}, 'workflow', 'Only stopped workflows can be resumed')
        state['status'] = 'active'
    else:
        require(False, 'event', 'Unknown recovery event', kind=kind)
    state['revision'] += 1
    return json.loads(canonical(state)), outbox
