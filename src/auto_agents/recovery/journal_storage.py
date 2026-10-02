"""Versioned journal encoding; one projection digest per event, not a copy."""
import base64
import hashlib
import zlib

from .model import checksum, require

FORMAT = 2
ENCODING = 'zjson-v1:'
PROJECTION = 'projection-sha256-v1:'


def pack(text):
    raw = text.encode()
    if len(raw) < 4096: return text
    compressed = zlib.compress(raw)
    encoded = ENCODING + hashlib.sha256(raw).hexdigest() + ':' + base64.b64encode(compressed).decode()
    return encoded if len(encoded) < len(raw) else text


def unpack(text):
    if not text.startswith(ENCODING): return text
    try:
        expected, payload = text[len(ENCODING):].split(':', 1)
        checksum(expected)
        raw = zlib.decompress(base64.b64decode(payload, validate=True))
        require(hashlib.sha256(raw).hexdigest() == expected, 'journal', 'Recovery journal integrity failure')
        return raw.decode()
    except (ValueError, UnicodeError, zlib.error) as error:
        from .model import KernelError
        raise KernelError('journal', 'Recovery journal integrity failure') from error


def projection(encoded):
    return PROJECTION + hashlib.sha256(encoded).hexdigest()


def stored_projection(db, rowid):
    if hasattr(db, 'blobopen'):
        with db.blobopen('kernel_events', 'result', rowid, readonly=True) as value:
            if len(value) != len(PROJECTION) + 64: return None
            result = value.read().decode()
    else:
        row = db.execute('SELECT CASE WHEN length(result)=? THEN result END FROM kernel_events WHERE rowid=?',
                         (len(PROJECTION) + 64, rowid)).fetchone()
        result = row[0] if row else None
    if not result or not result.startswith(PROJECTION): return None
    expected = result[len(PROJECTION):]
    checksum(expected)
    return expected


def digest_result(db, rowid):
    expected = stored_projection(db, rowid)
    if expected: return expected
    value = hashlib.sha256()
    if hasattr(db, 'blobopen'):
        with db.blobopen('kernel_events', 'result', rowid, readonly=True) as retained:
            for chunk in iter(lambda: retained.read(1024 * 1024), b''): value.update(chunk)
    else:
        value.update(db.execute('SELECT result FROM kernel_events WHERE rowid=?', (rowid,)).fetchone()[0].encode())
    return value.hexdigest()


def compact_state(state):
    """Bound display text, retaining all check identities and decision inputs."""
    from .observations import compact_for_storage
    def visit(value):
        if isinstance(value, dict):
            if (value.get('version') == 1 and isinstance(value.get('checks'), dict)
                    and 'command_id' in value and 'manifest' in value):
                return compact_for_storage(value)
            result = {key: visit(item) for key, item in value.items()}
            if 'kind' in result and 'details' in result and isinstance(result.get('reason'), str):
                result['reason'] = result['reason'][:1200]
            return result
        if isinstance(value, list): return [visit(item) for item in value]
        return value
    return visit(state)
