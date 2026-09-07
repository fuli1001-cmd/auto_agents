"""Local, model-free filesystem isolation for engine verification processes."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
from auto_agents import artifact_temp as tempfile
import sys
import ctypes


def landlock_abi():
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    return libc.syscall(444, None, 0, 1)


def restrict_nested_writes(roots):
    """Further restrict a verification child without creating another user ns.

    The outer Codex filesystem sandbox and private network namespace remain in
    force. Landlock adds an inherited write boundary for nested sandbox tests.
    ABI 3 is required so truncation and cross-directory rename are covered.
    """
    if landlock_abi() < 3:
        raise RuntimeError("nested verification requires Landlock ABI 3 or newer")
    class Ruleset(ctypes.Structure):
        _fields_ = [("handled_access_fs", ctypes.c_uint64)]
    class PathRule(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    writes = sum(1 << bit for bit in (1, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14))
    allowed = writes & ~((1 << 6) | (1 << 11))  # No device creation.
    rules = Ruleset(writes)
    fd = libc.syscall(444, ctypes.byref(rules), ctypes.sizeof(rules), 0)
    if fd < 0:
        raise OSError(ctypes.get_errno(), "could not create verification ruleset")
    try:
        # Git opens /dev/null read-write even for read-only repository probes.
        # Permit writes to that sink only, not arbitrary device files.
        for root, access in [*( (root, allowed) for root in roots), ("/dev/null", 1 << 1)]:
            path_fd = os.open(root, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = PathRule(access, path_fd)
                if libc.syscall(445, fd, 1, ctypes.byref(rule), 0) != 0:
                    raise OSError(ctypes.get_errno(), "could not bind verification write root")
            finally:
                os.close(path_fd)
        if libc.prctl(38, 1, 0, 0, 0) or libc.syscall(446, fd, 0):
            raise OSError(ctypes.get_errno(), "could not enforce verification write boundary")
    finally:
        os.close(fd)


def namespace_exec(payload):
    """Give tests a private /tmp as well as private PID/mount/network spaces."""
    kept = []
    try:
        for value in sorted(set(payload["preserve"]), key=lambda item: len(Path(item).parts)):
            path = Path(value).resolve()
            if str(path).startswith("/tmp/") and path.is_dir():
                kept.append((str(path), os.open(path, os.O_PATH | os.O_CLOEXEC)))
        subprocess.run([payload["ip"], "link", "set", "lo", "up"], check=True)
        subprocess.run([payload["mount"], "-t", "tmpfs", "-o", "mode=1777", "tmpfs", "/tmp"], check=True)
        for path, fd in kept:
            Path(path).mkdir(parents=True, exist_ok=True)
            subprocess.run([payload["mount"], "--no-canonicalize", "--rbind", f"/proc/self/fd/{fd}", path], pass_fds=(fd,), check=True)
        os.chdir(payload["cwd"])
    finally:
        for _, fd in kept:
            os.close(fd)
    os.execvp(payload["command"][0], payload["command"])


@contextmanager
def verification_argv(argv, cwd: Path, real_project: Path, *, read_roots=(), write_roots=()):
    root, target = Path(cwd).resolve(), Path(real_project).resolve()
    if root == target or root in target.parents or target in root.parents:
        raise RuntimeError("verification workspace overlaps the live target project")
    executable = shutil.which("codex")
    if not executable:
        raise RuntimeError("engine verification needs a local Codex sandbox executable; no model calls are made by this command")
    temporary_parent = os.environ.get("TMPDIR", "/tmp") if os.environ.get("AUTO_AGENTS_VERIFICATION_SANDBOX") else "/tmp"
    with tempfile.TemporaryDirectory(prefix="aav-", dir=temporary_parent) as temporary:
        scratch = Path(temporary)
        if target == scratch or target in scratch.parents:
            raise RuntimeError("verification scratch directory overlaps the live project")
        home = scratch / "home"
        codex_home = scratch / "codex-home"
        home.mkdir()
        codex_home.mkdir()
        (home / ".gitconfig").write_text("[user]\n name = auto-agents-verification\n email = verification@example.invalid\n", encoding="utf-8")
        entries = {":root": "read", "/tmp": "write", "/run": "deny",
                   str(root): "write", str(scratch): "write", str(target): "read"}
        preserve = [str(root), str(scratch), str(target), str(Path(__file__).resolve().parents[2])]
        writable = [str(root), str(scratch)]
        for value in write_roots:
            extra = Path(value).resolve()
            if extra == target or extra in target.parents or target in extra.parents or extra == Path("/"):
                raise RuntimeError("verification write root overlaps the live project")
            entries[str(extra)] = "write"
            preserve.append(str(extra))
            writable.append(str(extra))
        for value in read_roots:
            readonly = Path(value).resolve()
            if readonly == root or readonly in root.parents:
                raise RuntimeError("read-only verification input overlaps the writable workspace")
            entries[str(readonly)] = "read"
            preserve.append(str(readonly))
        for name in (".ssh", ".gnupg", ".codex"):
            sensitive = Path.home() / name
            if sensitive.exists():
                entries[str(sensitive)] = "deny"
        control = os.environ.get("AUTO_AGENTS_REPAIR_CONTROL_CONFIG")
        if control:
            config = json.loads(Path(control).read_text())
            entries[str(Path(config["root"]).resolve())] = "read"
            entries[str(root)] = "write"
            preserve.append(str(Path(config["root"]).resolve()))
        for directory in (root, target):
            if (directory / ".env").exists():
                entries[str(directory / ".env")] = "deny"
        for name in (".git", ".agents", ".codex"):
            if (root / name).exists():
                entries[str(root / name)] = "read"
        filesystem = ",".join(json.dumps(key) + "=" + json.dumps(value) for key, value in entries.items())
        profile = '{filesystem={' + filesystem + '},network={enabled=true}}'
        clean_environment = ["env", "-i", "PATH=" + os.environ.get("PATH", os.defpath),
                             "HOME=" + str(home), "CODEX_HOME=" + str(codex_home),
                             "TMPDIR=" + temporary, "LANG=C.UTF-8", "PYTHONPATH=" + str(root / "src"),
                             "PYTHONDONTWRITEBYTECODE=1",
                             "AUTO_AGENTS_TEST=True", "TESTING=True", "AUTO_AGENTS_REPAIR_CONTROL_DISABLED=1",
                             "AUTO_AGENTS_VERIFICATION_SANDBOX=1"]
        if os.environ.get("AUTO_AGENTS_VERIFICATION_SANDBOX"):
            yield [sys.executable, str(Path(__file__).resolve()), "--landlock", json.dumps(writable), *clean_environment, *argv]
            return
        sandbox = [executable, "sandbox", "-c", "features.network_proxy=false",
                   "-c", "permissions.autoagents_verify=" + profile,
                   "-P", "autoagents_verify", "-C", str(root), "--include-managed-config", "--", *clean_environment, *argv]
        if not os.environ.get("AUTO_AGENTS_VERIFICATION_SANDBOX"):
            unshare, ip, mount = shutil.which("unshare"), shutil.which("ip"), shutil.which("mount")
            if not unshare or not ip or not mount:
                raise RuntimeError("verification requires unshare, mount and ip for private test namespaces")
            payload = {"cwd": str(root), "preserve": preserve, "command": sandbox, "ip": ip, "mount": mount}
            sandbox = [unshare, "--user", "--map-root-user", "--mount", "--net", "--pid", "--fork", "--mount-proc",
                       sys.executable, str(Path(__file__).resolve()), "--namespace", json.dumps(payload)]
        yield ["env", "TMPDIR=" + temporary, *sandbox]


def check_verification_sandbox(root: Path, python: str, real_project: Path):
    """Fail before generation when the host cannot enforce the write boundary."""
    if landlock_abi() < 3:
        raise RuntimeError("verification host needs Landlock ABI 3 or newer")
    with tempfile.TemporaryDirectory(prefix="sandbox-probe-", dir=root) as probe:
        workspace, readonly = Path(probe) / "candidate", Path(probe) / "readonly"
        workspace.mkdir()
        readonly.mkdir()
        outside = readonly / "outside"
        code = (
            "from pathlib import Path; import socket; "
            "Path('inside').write_text('ok'); "
            "s=socket.socket(); s.bind(('127.0.0.1',0)); s.close(); "
            f"p=Path({str(outside)!r}); "
            "\ntry: p.write_text('not-allowed'); blocked=False\n"
            "except OSError: blocked=True\n"
            "assert blocked, 'sandbox allowed an out-of-workspace write'"
        )
        try:
            with verification_argv([python, "-c", code], workspace, readonly) as command:
                result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            if result.returncode:
                raise RuntimeError("verification sandbox is unavailable: " + result.stderr[-1000:])
        finally:
            # This is a uniquely named probe file, never a project input.
            outside.unlink(missing_ok=True)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--namespace":
        namespace_exec(json.loads(sys.argv[2]))
        raise SystemExit(3)
    if len(sys.argv) < 5 or sys.argv[1] != "--landlock":
        raise SystemExit("internal verification launcher requires --landlock ROOTS COMMAND")
    roots = json.loads(sys.argv[2])
    restrict_nested_writes(roots)
    os.chdir(roots[0])
    os.execvp(sys.argv[3], sys.argv[3:])
