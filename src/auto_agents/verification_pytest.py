"""Trusted pytest launcher and phase recorder; never loaded from a candidate."""
import json
from pathlib import Path
import sys
import time
if __package__:
    from .verification_inputs import InputObserver
    from .verification_dependencies import exception_dependencies, detect_verification_dependencies
else:
    from verification_inputs import InputObserver
    from verification_dependencies import exception_dependencies, detect_verification_dependencies


class Recorder:
    def __init__(self, root=None):
        self.root = root
        self.missing_dependencies = {}
        self.nodes = {}
        self.collected = []
        self.phases = {"setup": 0.0, "call": 0.0, "teardown": 0.0}
        self.started = time.monotonic()
        self.collection_seconds = 0.0

    def pytest_collection_finish(self, session):
        self.collected = [item.nodeid for item in session.items]
        self.collection_seconds = time.monotonic() - self.started

    def pytest_runtest_logreport(self, report):
        node = self.nodes.setdefault(report.nodeid, {"phases": {}, "seconds": 0.0})
        node["phases"][report.when] = report.outcome
        node["seconds"] += report.duration
        self.phases[report.when] = self.phases.get(report.when, 0.0) + report.duration

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
        return {"version": 1, "collected": self.collected, "passed": passed,
                "missing_dependencies": list(self.missing_dependencies.values()),
                "groups": sorted(groups.values(), key=lambda item: item["seconds"], reverse=True),
                "phases": {**self.phases, "collection": self.collection_seconds},
                "slowest": sorted(({"nodeid": node, **data} for node, data in self.nodes.items()),
                                  key=lambda data: data["seconds"], reverse=True)[:10]}


def main():
    root, output, *arguments = sys.argv[1:]
    sys.path.insert(0, str(Path(root) / "src"))
    # Verification belongs to this repository, never a parent directory's
    # pytest/conftest configuration in a transient checkout hierarchy.
    arguments.extend(["--rootdir=" + root, "--confcutdir=" + root])
    if not any(arg == "-c" or arg.startswith("--inifilename") for arg in arguments):
        config = next((Path(root) / name for name in ("pytest.ini", ".pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg")
                       if (Path(root) / name).is_file()), Path("/dev/null"))
        arguments.extend(["-c", str(config)])
    import pytest
    recorder = Recorder(root)
    observer = InputObserver(root)
    observer.start()
    code = pytest.main(arguments, plugins=[recorder])
    inputs = observer.finish()
    Path(output).write_text(json.dumps({**recorder.result(), "inputs": inputs}, sort_keys=True))
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())
