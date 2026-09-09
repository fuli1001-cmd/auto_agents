"""Extract executable pytest references from review prose without its punctuation."""
import re
from pathlib import PurePosixPath


_REFERENCE = re.compile(
    r'''(?P<quote>[`"'])(?P<quoted>(?:\./)?tests/[^\r\n]*?)(?P=quote)'''
    r'''|(?<![\w/.])(?:\./)?(?P<file>tests/[^\s`"',;()<>:\[\]]+\.py)''')
_NAME = re.compile(r'[\w./-]+')


def pytest_targets(text, *, prose=True):
    """Quoted references are literal; bare references may end a sentence.

    Parameter IDs can contain punctuation and whitespace. Never trim their
    contents, or turn an incomplete parameter selection into a whole-function
    selection. These references are later shell-quoted by the trusted executor.
    """
    result = []
    consumed = 0
    for match in _REFERENCE.finditer(text):
        if match.start() < consumed:
            continue
        if match.group('quoted') is not None:
            target = match.group('quoted')
            consumed = match.end()
        else:
            end = match.end()
            valid = True
            while text[end:end + 2] == '::':
                name = _NAME.match(text, end + 2)
                if name is None:
                    valid = False
                    break
                end = name.end()
                if prose and text[end:end + 1] not in {'[', ':'}:
                    end -= len(name.group()) - len(name.group().rstrip('.'))
                if end == name.start():
                    valid = False
                    break
                if text[end:end + 1] == '[':
                    depth = 1
                    end += 1
                    while end < len(text) and text[end] not in '\r\n' and depth:
                        depth += (text[end] == '[') - (text[end] == ']')
                        end += 1
                    if depth:
                        valid = False
                        break
            consumed = end
            if not valid:
                continue
            following = text[end:end + 1]
            if following and (following.isalnum() or following in '_/'
                              or following == '.' and text[end + 1:end + 2].isalnum()):
                continue
            target = text[match.start():end]
        target = target.removeprefix('./')
        path = target.split('::', 1)[0]
        if not path.endswith('.py') or '..' in PurePosixPath(path).parts:
            continue
        if target not in result:
            result.append(target)
    return result
