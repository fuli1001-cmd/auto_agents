"""Structured operator decisions. Secret values never enter business receipts."""

from ..operator_inputs import UserInputRequest, OperatorInputStore
from .types import ControlError, digest


def read_multiline(prompt, *, stream=None, output=None):
    """Read one terminal submission through EOF, without replacing stdin."""
    import os
    import sys

    stream = sys.stdin if stream is None else stream
    output = sys.stderr if output is None else output
    submit = "Ctrl+Z 后按 Enter" if os.name == "nt" else "Ctrl+D"
    print(prompt + "\n可输入多行；输入完成后，换行并按 " + submit + " 提交；Ctrl+C 取消。",
          file=output, flush=True)
    lines = []
    while True:
        try:
            line = stream.readline()
        except EOFError:
            break
        if not line:
            break
        lines.append(line)
        if not line.endswith("\n"):
            # readline returns a non-terminated final line only after EOF;
            # do not start another terminal read and demand another Ctrl+D.
            break
    return "".join(lines).strip()


def owned_work(store, root):
    queue = [root]
    seen = set()
    while queue:
        work = queue.pop()
        if work["id"] in seen:
            raise ControlError("work_cycle", "Owned work graph contains a cycle")
        seen.add(work["id"])
        yield work
        child_ids = work["context"].get("children", []) or (
            [work["context"]["child"]] if work["context"].get("child") else []
        )
        if work["context"].get("prototype_addon"):
            child_ids = [*child_ids, work["context"]["prototype_addon"]]
        for identity in child_ids:
            child = store.work(identity)
            if child["parent"] != work["id"]:
                raise ControlError("child_authority", "Child belongs to another parent")
            queue.append(child)


def pending_decisions(store, root):
    import shlex
    from ..diagnostic_redaction import sanitize

    result = []
    for work in owned_work(store, root):
        if work["status"] != "WAITING":
            continue
        state = work["context"]
        if state.get("prototype_addon"):
            continue
        prefix = ["auto-agents", "--project", str(store.project), "--session", work["id"]]
        if state.get("waiting_for") == "user":
            for raw in state.get("input_requests", []):
                request = UserInputRequest.from_dict(raw)
                command = [prefix[0], "answer", *prefix[1:], "--request-id", request.request_id]
                command += ["--no-resume"]
                answer_option = ' --from-env AUTO_AGENTS_ANSWER' if request.kind == "secret" or request.sensitivity == "secret" else ' --value "<你的回答>"'
                result.append({"work_id": work["id"], "kind": "user",
                    "question": sanitize(request.question), "request_id": request.request_id,
                    "command": shlex.join(command) + answer_option})
        elif state.get("waiting_for") == "approval":
            gate = state.get("approval", "")
            command = [prefix[0], "approve", *prefix[1:], "--gate", gate]
            decision = {"work_id": work["id"], "kind": "approval",
                "question": "请审阅并批准：" + gate, "gate": gate,
                "command": shlex.join(command)}
            if gate == "prototype":
                decision.update(
                    scope_summary=state.get("prototype_scope_summary", "待补做范围验证；恢复会话后才可批准"),
                    baseline_repairs=state.get("prototype_baseline_repairs", "待核对"),
                    preview_command=shlex.join([prefix[0], "prototype", "preview", *prefix[1:]]),
                    preview_instructions="端口自动选择，无需指定。启动后在浏览器打开终端打印的地址，"
                    "可并排比较原型；按 Ctrl+C 退出预览后再审批。",
                    reject_command=shlex.join([
                        prefix[0], "reject", *prefix[1:], "--gate", gate,
                        "--reason", "请填写拒绝原因及需要保留或恢复的功能",
                    ]),
                    reject_instructions="用 --reason 填写具体意见；拒绝后执行恢复命令重新设计，"
                    "新原型仍需审阅，原会话和已消耗调用次数保留。",
                )
            result.append(decision)
    return result


def resolved_answers(project, answers):
    """Project ordinary decisions into prompts; secret values stay in bindings."""
    store = OperatorInputStore(project)
    result = []
    for answer in answers:
        if not isinstance(answer, dict) or not answer.get("key"):
            result.append(answer)
            continue
        record = store.get(answer["key"])
        if not record or answer.get("revision") and answer["revision"] != record.get("revision"):
            raise ControlError("operator_input_changed", "Recorded operator answer changed; reconcile it before continuing",
                category="operator", details={"key": answer["key"]})
        projected = dict(answer)
        if record.get("kind") == "secret" or record.get("sensitivity") == "secret":
            projected["available"] = bool(record.get("present"))
        elif "value" in record:
            projected["value"] = record["value"]
        result.append(projected)
    return result


def pending_questions(project, work, questions):
    if not isinstance(questions, list):
        questions = [questions]
    requests = []
    bindings = []
    store = OperatorInputStore(project)
    if not questions or any(value is None for value in questions):
        raise ControlError("model_output", "Operator question is empty", category="model")
    for index, value in enumerate(questions):
        if not isinstance(value, dict):
            value = {
                "key": "decision." + digest([work["id"], work["phase"], index])[:16],
                "kind": "text",
                "question": str(value),
                "purpose": work["phase"],
                "stage": work["phase"],
                "task_id": work["id"],
                "why_required": "The current task needs an operator decision",
            }
        if not isinstance(value.get("question"), str) or not value["question"].strip():
            raise ControlError("model_output", "Operator question is empty", category="model")
        try:
            request = UserInputRequest.from_dict(value)
        except (ValueError, TypeError) as error:
            raise ControlError("model_output", str(error), category="model") from error
        if not request.question:
            raise ControlError(
                "model_output", "Operator question is empty", category="model"
            )
        valid, _ = store.is_valid(request)
        bindings.extend(request.bindings)
        if not valid:
            requests.append(request.to_dict())
    return requests, bindings
