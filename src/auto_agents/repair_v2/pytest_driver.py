"""Installed by the controller image, outside the candidate source tree."""
import json
import os
from pathlib import Path
import sys

import pytest


class Evidence:
    def __init__(self):
        self.collected, self.passed, self.failed, self.skipped = [], [], [], []
        self.call_failed = []

    def pytest_collection_finish(self, session):
        self.collected = [item.nodeid for item in session.items]

    def pytest_runtest_logreport(self, report):
        if report.failed: self.failed.append(report.nodeid)
        if report.skipped or hasattr(report, 'wasxfail'): self.skipped.append(report.nodeid)
        if report.when == 'call' and report.passed and not hasattr(report, 'wasxfail'):
            self.passed.append(report.nodeid)

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_makereport(self, item, call):
        outcome = yield
        report = outcome.get_result()
        # A call-phase failure alone does not prove the test body failed:
        # strict XPASS has no exception, and unittest reports its fixtures in
        # this phase too. Require the collected callable in the exception's
        # traceback. Unknown/custom items fail closed for counterexample use.
        code = getattr(getattr(item, 'obj', None), '__code__', None)
        if (call.when == 'call' and call.excinfo is not None
                and report.failed and not hasattr(report, 'wasxfail')
                and code is not None
                and any(entry.frame.code.raw is code for entry in call.excinfo.traceback)):
            self.call_failed.append(report.nodeid)


def main():
    home = Path(os.environ['HOME'])
    home.mkdir(parents=True, exist_ok=True)
    (home / '.gitconfig').write_text('[user]\n name = auto-agents verification\n email = verification@localhost\n')
    evidence = Evidence()
    sys.path.insert(0, '/work')
    sys.path.insert(0, '/work/src')
    if Path('/work/src/auto_agents').is_dir():
        import importlib.util
        spec = importlib.util.find_spec('auto_agents')
        if spec is None or not spec.origin or not Path(spec.origin).resolve().is_relative_to(Path('/work/src')):
            raise RuntimeError('candidate imports resolve outside the frozen source')
    args = json.loads(os.environ['REPAIR_PYTEST_ARGS'])
    code = int(pytest.main([*args, '-p', 'no:cacheprovider'], plugins=[evidence]))
    data = {'returncode': code, 'collected': evidence.collected, 'passed': evidence.passed,
            'failed': evidence.failed, 'skipped': evidence.skipped, 'call_failed': evidence.call_failed}
    Path('/result/pytest.json').write_text(json.dumps(data))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
