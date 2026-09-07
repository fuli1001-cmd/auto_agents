"""Trusted pytest launcher and phase recorder; never loaded from a candidate."""
import json
from pathlib import Path
import sys
import time
if __package__:
    from .verification_inputs import InputObserver
else:
    from verification_inputs import InputObserver


class Recorder:
    def __init__(self):
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
    recorder = Recorder()
    observer = InputObserver(root)
    observer.start()
    code = pytest.main(arguments, plugins=[recorder])
    inputs = observer.finish()
    Path(output).write_text(json.dumps({**recorder.result(), "inputs": inputs}, sort_keys=True))
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())
