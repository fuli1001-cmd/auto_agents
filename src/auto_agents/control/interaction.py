"""Structured operator decisions. Secret values never enter business receipts."""

from ..operator_inputs import UserInputRequest, OperatorInputStore
from .types import ControlError, digest


def pending_questions(project, work, questions):
    if not isinstance(questions, list):
        questions = [questions]
    requests = []
    bindings = []
    store = OperatorInputStore(project)
    for index, value in enumerate(questions):
        if not isinstance(value, dict):
            value = {
                "key": "decision." + digest([work["id"], work["phase"], index])[:16],
                "kind": "text",
                "question": str(value),
                "purpose": work["phase"],
                "why_required": "The current task needs an operator decision",
            }
        request = UserInputRequest.from_dict(value)
        if not request.question:
            raise ControlError(
                "model_output", "Operator question is empty", category="model"
            )
        valid, _ = store.is_valid(request)
        bindings.extend(request.bindings)
        if not valid:
            requests.append(request.to_dict())
    return requests, bindings
