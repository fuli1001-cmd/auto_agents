"""Installed by the controller image, outside the candidate source tree."""
import json
import os
from pathlib import Path
import sys

import pytest


class Evidence:
    def __init__(self):
        self.collected, self.passed, self.failed, self.skipped = [], [], [], []

    def pytest_collection_finish(self, session):
        self.collected = [item.nodeid for item in session.items]

    def pytest_runtest_logreport(self, report):
        if report.failed: self.failed.append(report.nodeid)
        if report.skipped or hasattr(report, 'wasxfail'): self.skipped.append(report.nodeid)
        if report.when == 'call' and report.passed and not hasattr(report, 'wasxfail'):
            self.passed.append(report.nodeid)


def main():
    evidence = Evidence()
    sys.path.insert(0, '/work')
    sys.path.insert(0, '/work/src')
    args = json.loads(os.environ['REPAIR_PYTEST_ARGS'])
    code = int(pytest.main([*args, '-p', 'no:cacheprovider'], plugins=[evidence]))
    data = {'returncode': code, 'collected': evidence.collected, 'passed': evidence.passed,
            'failed': evidence.failed, 'skipped': evidence.skipped}
    Path('/result/pytest.json').write_text(json.dumps(data))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
