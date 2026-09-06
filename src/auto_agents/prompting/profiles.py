"""Small, reviewed deltas. Unknown/new model versions deliberately use generic.

Reviewed 2026-09-06. Sources:
https://developers.openai.com/api/docs/guides/latest-model?model=gpt-6-astra
https://developers.openai.com/api/docs/guides/latest-model?model=gpt-5.6
https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices
https://ai.google.dev/gemini-api/docs/whats-new-gemini-3.5
"""
import re

from .core import IMPLEMENT, PromptBlock


def profile_rules(model: str, purpose: str):
    name = model.lower().removesuffix("[1m]")
    if name == "gpt-6-astra":
        profile = "gpt-6-astra"
        rule = "Within this stage's authorized scope, perform the work rather than offering to do it. " \
               "A routine implementation choice is not a new approval boundary."
    elif name in {"gpt-5.6", "gpt-5.6-sol"}:
        profile = "gpt-5.6-sol"
        rule = "Keep the summary focused while preserving every required proof field, material blocker and next action."
    elif re.fullmatch(r"claude-(?:sonnet|opus)-4[.-]6(?:-\d{8})?", name):
        profile = "claude-4.6"
        rule = "Use tools when they resolve a concrete uncertainty. Stop exploration once the owned task has sufficient evidence."
    elif name in {"claude-sonnet-5", "claude-opus-5", "claude-fable-5.1", "claude-fable-5-1"}:
        profile = name.replace("5-1", "5.1")
        rule = "Apply the complete task contract to every owned item. Avoid extra verification agents, " \
               "unrequested extensions and redundant permanent tests. Finish the authorized stage before returning."
    elif re.fullmatch(r"gemini-3(?:\.1|\.5)?-(?:flash|pro|flash-lite)(?:-preview)?", name):
        profile = "gemini-3.x"
        rule = "Use the preceding task data to complete the current stage directly. " \
               "Choose the necessary evidence and avoid repeated exploration without new information."
    else:
        return "generic", ()
    # Pure machine decisions need their exact protocol, not conversational nudges.
    if purpose not in IMPLEMENT | {"readme", "design", "plan", "prototype"}:
        return profile, ()
    return profile, (PromptBlock(rule, "model." + profile),)
