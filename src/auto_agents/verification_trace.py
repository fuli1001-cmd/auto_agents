"""Resolve strace file paths without guessing process cwd or directory fds."""
import json
from pathlib import Path
import re


def _arguments(text):
    result, start, depth, quoted, escaped = [], 0, 0, False, False
    for index, char in enumerate(text):
        if escaped:
            escaped = False
        elif quoted and char == "\\":
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif not quoted:
            if char in "([{":
                depth += 1
            elif char in ")]}":
                depth -= 1
            elif char == "," and depth == 0:
                result.append(text[start:index].strip())
                start = index + 1
    result.append(text[start:].strip())
    return result


def resolve_file_trace(text, root):
    """Return canonical path-only records, or None for an incomplete trace."""
    pending, rows = {}, []
    for index, line in enumerate(text.splitlines()):
        match = re.match(r"^(?:\[pid\s+)?(\d+)\]?\s+(.*)$", line)
        pid, body = (match.group(1), match.group(2)) if match else ("0", line.strip())
        if "<unfinished ...>" in body:
            pending[pid] = (index, body.replace("<unfinished ...>", ""))
            continue
        resumed = re.match(r"<\.\.\. \w+ resumed>(.*)", body)
        if resumed:
            if pid not in pending:
                return None
            index, prefix = pending.pop(pid)
            body = prefix + resumed.group(1)
        rows.append((index, pid, body))
    if pending:
        return None
    contexts, output = {}, []
    shared_fs = False
    at_calls = {"openat", "openat2", "newfstatat", "fstatat64", "statx", "faccessat", "faccessat2",
                "readlinkat", "mkdirat", "unlinkat", "mknodat", "fchmodat", "fchownat", "utimensat", "execveat"}
    single = {"open", "stat", "lstat", "access", "readlink", "execve", "mkdir", "unlink", "rmdir",
              "chmod", "chown", "lchown", "utime", "utimes", "truncate", "statfs", "mknod"}
    network = {"connect", "sendto", "sendmsg", "recvfrom", "recvmsg", "accept", "accept4", "socket", "socketpair",
               "bind", "listen", "getsockname", "getpeername", "setsockopt", "getsockopt", "shutdown"}
    ignored = {"exit", "exit_group", "wait4", "waitid", "kill", "tgkill", "close", "fcntl", "dup", "dup2", "dup3"}
    def descriptor(value):
        found = re.search(r"<(/[^>]+)>", value)
        if not found or "\\" in found.group(1) or found.group(1).endswith(" (deleted)"):
            raise ValueError("unresolved fd")
        return Path(found.group(1))
    try:
        for _, pid, body in sorted(rows):
            if body.startswith(("+++", "---")) or not body:
                continue
            match = re.match(r"(\w+)\((.*)\)\s+=\s+(.*)$", body)
            if not match:
                return None
            name, raw, returned = match.groups()
            args = _arguments(raw)
            if pid not in contexts:
                if contexts:
                    return None
                contexts[pid] = {"cwd": Path(root)}
            context = contexts[pid]
            def path(index, fd=None):
                value = json.loads(args[index])
                if not isinstance(value, str):
                    raise ValueError("not a path")
                if not value and "AT_EMPTY_PATH" in raw and fd is not None:
                    return descriptor(args[fd])
                base = context["cwd"] if fd is None or args[fd] == "AT_FDCWD" else descriptor(args[fd])
                return Path(value) if Path(value).is_absolute() else base / value
            paths = []
            if name in {"clone", "clone3", "fork", "vfork"}:
                child = re.match(r"([1-9]\d*)", returned)
                if child:
                    share = "CLONE_FS" in raw
                    shared_fs = shared_fs or share
                    contexts[child.group(1)] = context if share else dict(context)
                continue
            if name in {"unshare", "setns", "mount", "umount2", "chroot", "pivot_root"}:
                return None
            if name in {"chdir", "fchdir"}:
                if shared_fs:
                    return None
                destination = path(0) if name == "chdir" else descriptor(args[0])
                paths = [destination]
                if returned.startswith("0"):
                    context["cwd"] = destination
            elif name in at_calls:
                if args[1] == '""' and "AT_EMPTY_PATH" in raw and re.fullmatch(r"\d+<(?:pipe|socket):\[\d+\]>", args[0]):
                    paths = []
                else:
                    paths = [path(1, 0)]
            elif name in single:
                paths = [path(0)]
            elif name in {"rename", "link", "symlink"}:
                paths = [path(0), path(1)]
            elif name in {"renameat", "renameat2", "linkat"}:
                paths = [path(1, 0), path(3, 2)]
            elif name in {"fstat", "fstatfs", "getdents64"}:
                paths = [] if re.fullmatch(r"\d+<(?:pipe|socket):\[\d+\]>", args[0]) else [descriptor(args[0])]
            elif name in network:
                output.append("connect()")
            elif name not in ignored:
                return None
            for value in paths:
                output.append("stat(" + json.dumps(str(value)) + ") = " + returned)
        return "\n".join(output)
    except (ValueError, IndexError, TypeError):
        return None
