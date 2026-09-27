"""Protocol 2: explicit aggregate revisions and runtime fencing on mutations."""
from .model import Event, PROTOCOL, require


def dispatch(store, request):
    require(request.get('version') == PROTOCOL, 'rpc_protocol', 'Unsupported recovery protocol')
    operation = request.get('op')
    if operation == 'kernel-status': return {'ok':True, 'protocol':PROTOCOL, 'epoch':store.meta('epoch',0), **store.status()}
    if operation == 'kernel-replay': return {'ok':True, 'state':store.replay(request['stream'])}
    require(operation == 'kernel-event', 'rpc_operation', 'Unknown recovery operation')
    require(type(request.get('epoch')) is int and type(request.get('revision')) is int,
            'rpc_fence', 'Mutation requires runtime epoch and expected aggregate revision')
    state = store.apply(request['stream'], request['revision'], Event(**request['event']), expected_epoch=request['epoch'])
    return {'ok':True,'state':state,'epoch':request['epoch']}
