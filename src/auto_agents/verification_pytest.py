"""Trusted pytest launcher and phase recorder; never loaded from a candidate."""
import json
from pathlib import Path
import shlex
import sys
import time
if __package__:
    from .verification_inputs import InputObserver
    from .verification_dependencies import exception_dependencies, detect_verification_dependencies
else:
    from verification_inputs import InputObserver
    from verification_dependencies import exception_dependencies, detect_verification_dependencies


def prepare_execution_receipts(command, checkout, scratch, environment):
    """Instrument retained Python pytest invocations without changing selectors."""
    from .execution_binding import test_invocations, RunnerContextError
    replacements, reports = [], []
    offset = 0
    for invocation in test_invocations(command, environment=environment):
        if invocation.runner != 'pytest':
            continue
        if invocation.targets is None or invocation.launcher[-2:] != ('-m', 'pytest'):
            raise RunnerContextError('execution_receipt', 'fresh pytest receipt requires a Python module launcher', invocation.raw)
        start = command.index(invocation.raw, offset)
        end = start + invocation.option_offset
        offset = start + len(invocation.raw)
        report = scratch / f'pytest-execution-{len(reports)}.json'
        launcher = []
        for token in invocation.launcher[:-2]:
            if '=' in token and token.split('=', 1)[0].isidentifier():
                name, value = token.split('=', 1)
                launcher.append(name + '=' + shlex.quote(value))
            else:
                launcher.append(shlex.quote(token))
        launcher.extend(shlex.quote(str(value)) for value in
                        (Path(__file__).resolve(), '--module-invocation', checkout / invocation.cwd, report))
        replacements.append((start, end, ' '.join(launcher) + ' '))
        reports.append(report)
    for start, end, replacement in reversed(replacements):
        command = command[:start] + replacement + command[end:]
    return command, reports


class Recorder:
    def __init__(self, root=None, output=None):
        self.root = root
        self.output = output
        self.failures = []
        self.missing_dependencies = {}
        self.nodes = {}
        self.collected = []
        self.phases = {"setup": 0.0, "call": 0.0, "teardown": 0.0}
        self.started = time.monotonic()
        self.collection_seconds = 0.0

    def checkpoint(self, identity):
        if self.output is not None:
            path = Path(str(self.output) + ".progress.json")
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"checkpoints": [identity], "failures": self.failures}))
            temporary.replace(path)

    def pytest_runtest_logstart(self, nodeid, location):
        self.checkpoint("start:" + nodeid)

    def pytest_collection_finish(self, session):
        self.collected = [item.nodeid for item in session.items]
        self.collection_seconds = time.monotonic() - self.started
        self.checkpoint("collected:" + str(len(self.collected)))

    def pytest_runtest_logreport(self, report):
        node = self.nodes.setdefault(report.nodeid, {"phases": {}, "seconds": 0.0})
        node["phases"][report.when] = report.outcome
        node["seconds"] += report.duration
        self.phases[report.when] = self.phases.get(report.when, 0.0) + report.duration
        if report.failed:
            self.failures.append({"nodeid": report.nodeid, "phase": report.when,
                                  "detail": str(report.longreprtext)})
        self.checkpoint(f"{report.nodeid}:{report.when}:{report.outcome}")

    def pytest_exception_interact(self, node, call, report):
        if not report.failed or call.excinfo is None:
            return
        error = call.excinfo.value
        evidence = f"{type(error).__name__}: {error}"
        for stream in ("stdout", "stderr"):
            value = getattr(error, stream, "") or ""
            evidence += "\n" + (value.decode(errors="replace") if isinstance(value, bytes) else str(value))
        dependencies = [*exception_dependencies(error, self.root),
                        *detect_verification_dependencies(evidence, workspace=self.root)]
        for dependency in dependencies:
            self.missing_dependencies[dependency.key] = {**dependency.to_dict(), "nodeid": report.nodeid, "phase": report.when}

    def result(self):
        passed = [node for node, data in self.nodes.items()
                  if all(data["phases"].get(phase) == "passed" for phase in ("setup", "call", "teardown"))]
        groups = {}
        for node, data in self.nodes.items():
            name = node.split("::", 1)[0]
            group = groups.setdefault(name, {"file": name, "seconds": 0.0, "tests": 0})
            group["seconds"] += data["seconds"]
            group["tests"] += 1
        return {"version": 2, "failures": self.failures, "collected": self.collected, "passed": passed,
                "missing_dependencies": list(self.missing_dependencies.values()),
                "groups": sorted(groups.values(), key=lambda item: item["seconds"], reverse=True),
                "phases": {**self.phases, "collection": self.collection_seconds},
                "slowest": sorted(({"nodeid": node, **data} for node, data in self.nodes.items()),
                                  key=lambda data: data["seconds"], reverse=True)[:10]}


def main():
    arguments = sys.argv[1:]
    module_invocation = arguments[:1] == ['--module-invocation']
    if module_invocation:
        arguments = arguments[1:]
    root, output, *arguments = arguments
    if module_invocation:
        # Trusted recorder dependencies were imported above from this script's
        # directory. Restore the original `python -m pytest` import path before
        # importing pytest or any project code; do not inject an undeclared src/.
        if not getattr(sys.flags, 'safe_path', False):
            sys.path[0] = str(Path.cwd())
    else:
        # Preserve the existing explicit launcher contract for its other users.
        sys.path.insert(0, str(Path(root) / "src"))
    # Verification belongs to this repository, never a parent directory's
    # pytest/conftest configuration in a transient checkout hierarchy.
    arguments.extend(["--rootdir=" + root, "--confcutdir=" + root])
    if not any(arg == "-c" or arg.startswith("--inifilename") for arg in arguments):
        config = next((Path(root) / name for name in ("pytest.ini", ".pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg")
                       if (Path(root) / name).is_file()), Path("/dev/null"))
        arguments.extend(["-c", str(config)])
    import pytest
    recorder = Recorder(root, output)
    observer = InputObserver(root)
    observer.start()
    code = pytest.main(arguments, plugins=[recorder])
    inputs = observer.finish()
    Path(output).write_text(json.dumps({**recorder.result(), "inputs": inputs}, sort_keys=True))
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())
