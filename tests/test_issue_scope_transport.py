import json

import pytest

from auto_agents.workflow_chain import IssueBriefBuilder


@pytest.mark.parametrize('scope', [
    {'task_id': 'owned-task'},
    {'task_ids': ['owned-task', 'prerequisite-task']},
    {'requirement_ids': ['REQ-owned']},
    {},
])
def test_issue_materialization_preserves_explicit_scope_without_inventing_it(tmp_path, scope):
    builder = IssueBriefBuilder(tmp_path, 'child')
    paths = builder.materialize({'summary': 'Existing defect', **scope})
    issue = json.loads((tmp_path / paths['json']).read_text())
    assert {key: issue[key] for key in ('task_id', 'task_ids', 'requirement_ids') if key in issue} == scope
    assert issue['summary'] == 'Existing defect'
