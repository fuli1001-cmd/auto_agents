"""Small behavioral counterexamples drawn from the reported repair defects."""
OWNERSHIP_SOURCE = '''from command_selector import compile_command

def verify(commands, owners, probe, run):
    results = []
    for original in commands:
        command = compile_command(original)
        if not probe(command):
            return {"ok": False, "results": [], "owner": owners.get(command)}
        ok = run(command)
        results.append({"command": command, "owner": owners.get(command), "ok": ok})
    return {"ok": all(row["ok"] for row in results), "results": results}
'''
OWNERSHIP_SELECTOR = '''import shlex

def compile_command(command):
    args = shlex.split(command)
    if len(args) > 3 and args[:3] == ["npx", "vitest", "run"] and "::" in args[3]:
        path, pattern = args[3].split("::", 1)
        args[3] = path
        args += ["-t", pattern]
    return shlex.join(args)
'''
OWNERSHIP_TESTS = '''import re
import shlex
from source import verify
from command_selector import compile_command

def test_literal_selector_preserved():
    literal = "suite[1].*"
    args = shlex.split(compile_command("npx vitest run owned.test.ts::" + literal))
    assert args[:4] == ["npx", "vitest", "run", "owned.test.ts"]
    pattern = args[args.index("-t") + 1]
    assert re.fullmatch(pattern, literal)
    assert not re.fullmatch(pattern, "suite1anything")

def test_compilation_keeps_original_command_owner():
    original = "npx vitest run owned.test.ts::suite.*"
    result = verify([original], {original: "REQ-A"}, lambda _: True, lambda _: True)
    assert result["ok"]
    assert result["results"][0]["owner"] == "REQ-A"

def test_preflight_failure_keeps_completed_results_and_failed_owner():
    first, second = "npx vitest run first.test.ts", "npx vitest run missing.test.ts::case+"
    calls = []
    result = verify([first, second], {first: "REQ-A", second: "REQ-B"},
                    lambda command: "missing.test.ts" not in command, lambda command: calls.append(command) or True)
    assert not result["ok"]
    assert result["owner"] == "REQ-B"
    assert len(result["results"]) == 1 and result["results"][0]["owner"] == "REQ-A"
    assert calls == [first]

def test_same_file_selectors_keep_distinct_owners():
    first, second = "npx vitest run same.test.ts::alpha.*", "npx vitest run same.test.ts::beta+"
    result = verify([first, second], {first: "REQ-A", second: "REQ-B"}, lambda _: True, lambda _: True)
    assert [row["owner"] for row in result["results"]] == ["REQ-A", "REQ-B"]
'''
OWNERSHIP_REQUIREMENTS = (
    ('literal-selectors', 'Vitest file::test selectors match the literal test name, preserving regex metacharacters.'),
    ('command-owners', 'Compiled commands and preflight failures retain the owner of their exact original command.'),
    ('partial-accounting', 'A later preflight failure preserves already completed results and never executes the failed command.'),
)
