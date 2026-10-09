"""Pure goal recognition; execution constraints are tested through the public controller."""

import pytest
from auto_agents.collab_goal_scope import acceptance_only

GOAL = "对当前已有功能执行真实端到端验收，通过前端创建视频，不新增产品需求，不重新设计或规划项目。"


@pytest.mark.parametrize(
    "goal, expected",
    [
        (GOAL, True),
        ("Run acceptance of existing behavior, no new features or planning.", True),
        ("Build a new video application with five new features.", False),
        ("Implement the approved missing recovery capability.", False),
        ("Review existing behavior and propose new product requirements.", False),
    ],
)
def test_scope_comes_from_explicit_user_goal(goal, expected):
    assert acceptance_only(goal) is expected
