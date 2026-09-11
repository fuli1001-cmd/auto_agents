"""Replayable metadata reads for the trusted Python input observer."""
import hashlib
import json
import os
from pathlib import Path

# NUL cannot occur in an actual filename, so ordinary read manifests cannot
# collide with a metadata observation.
PREFIX = '\0filesystem-probe:'
OPERATIONS = {name: getattr(os, name) for name in ('stat', 'lstat', 'access', 'readlink', 'getcwd')}
PATH_METHODS = {name: getattr(type(Path()), name) for name in ('__fspath__', '__str__')}


def descriptor(root, operation, args, kwargs):
    if operation == 'getcwd':
        if args or kwargs:
            raise ValueError('unsupported cwd probe')
        return {'operation': operation}
    # Arbitrary __fspath__ callbacks or directory descriptors can hide inputs.
    if not args or type(args[0]) not in {str, bytes, type(Path())}:
        raise ValueError('unresolved probe path')
    if type(args[0]) is type(Path()) and any(getattr(type(args[0]), name) is not method
                                           for name, method in PATH_METHODS.items()):
        raise ValueError('modified path protocol')
    path = Path(os.fsdecode(args[0]))
    if '..' in path.parts:
        raise ValueError('relative parent probe')
    path = path if path.is_absolute() else root / path
    external = not path.is_relative_to(root)
    location = str(path) if external else path.relative_to(root).as_posix()
    options = dict(kwargs)
    if options.pop('dir_fd', None) is not None:
        raise ValueError('descriptor-relative probe')
    if operation == 'access':
        if len(args) != 2 or type(args[1]) is not int or not 0 <= args[1] <= 7:
            raise ValueError('unsupported access mode')
        options['mode'] = args[1]
        allowed = {'mode', 'effective_ids', 'follow_symlinks'}
    else:
        if len(args) != 1:
            raise ValueError('unsupported probe arguments')
        allowed = {'follow_symlinks'} if operation == 'stat' else set()
    if set(options) - allowed or any(type(value) is not bool for key, value in options.items() if key != 'mode'):
        raise ValueError('unsupported probe options')
    return {'operation': operation, 'path': location, 'external': external, 'options': options}


def value(result=None, error=None):
    if error is not None:
        payload = ['errno', error.errno]
    elif isinstance(result, os.stat_result):
        payload = ['stat', {name: getattr(result, name) for name in dir(result) if name.startswith('st_')}]
    elif isinstance(result, bytes):
        payload = ['bytes', result.hex()]
    else:
        payload = ['value', result]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def matches(root, key, expected):
    try:
        record = json.loads(key[len(PREFIX):])
        operation = record['operation']
        if operation not in OPERATIONS:
            return False
        if operation == 'getcwd':
            # Managed commands start at root; a subsequent chdir invalidates
            # the observer independently.
            return record == {'operation': operation} and value(str(root)) == expected
        location = record['path']
        path = Path(location) if record.get('external') else root / location
        options = dict(record.get('options', {}))
        args = (str(path), options.pop('mode')) if operation == 'access' else (str(path),)
        if descriptor(root, operation, args, options) != record:
            return False
        try:
            observed = value(OPERATIONS[operation](*args, **options))
        except OSError as error:
            observed = value(error=error)
        return observed == expected
    except (OSError, ValueError, TypeError, KeyError):
        return False
