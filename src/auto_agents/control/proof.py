"""Inspect test launches without executing or rewriting the owned command."""

from pathlib import Path
import re
import shlex

from ..execution_binding import test_invocations, command_spans, executable_tokens


def launches(command):
    result = []
    cwd = "."
    for start, end in command_spans(command):
        raw = command[start:end].strip()
        words = executable_tokens(raw)
        if words[:1] == ["cd"] and len(words) == 2:
            cwd = str(Path(cwd) / words[1])
            continue
        if words and re.fullmatch(
            r"python(?:\d+(?:\.\d+)*)?(?:\.exe)?", Path(words[0]).name
        ):
            index = 1
            while index < len(words) and words[index] in {"-B", "-u", "-E", "-s", "-I"}:
                index += 1
            if words[index : index + 2] == ["-m", "unittest"]:
                args = words[index + 2 :]
                targets = []
                if "discover" in args:
                    directory, pattern = "tests", "test*.py"
                    cursor = args.index("discover") + 1
                    while cursor < len(args):
                        option = args[cursor]
                        if option in {
                            "-s",
                            "--start-directory",
                            "-p",
                            "--pattern",
                            "-t",
                            "--top-level-directory",
                        }:
                            cursor += 1
                            if cursor == len(args):
                                break
                            if option in {"-s", "--start-directory"}:
                                directory = args[cursor]
                            if option in {"-p", "--pattern"}:
                                pattern = args[cursor]
                        elif not option.startswith("-"):
                            directory = option
                        cursor += 1
                    targets = [str(Path(cwd) / directory / pattern)]
                else:
                    targets = [
                        str(Path(cwd) / (arg.replace(".", "/") + ".py"))
                        for arg in args
                        if not arg.startswith("-")
                    ]
                result.append(
                    {
                        "runner": "unittest",
                        "targets": targets,
                        "discovery": "discover" in args,
                    }
                )
                continue
            if words[index : index + 2] == ["-m", "pytest"]:
                # Harmless interpreter switches are not runner selectors.
                raw = shlex.join([words[0], *words[index:]])
        for invocation in test_invocations(raw):
            result.append(
                {
                    "runner": invocation.runner,
                    "targets": [
                        str(Path(cwd) / x) for x in invocation.repository_targets
                    ],
                    "discovery": not invocation.targets,
                    "known": invocation.targets is not None,
                }
            )
    return result


def executed_count(output):
    output = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', output)
    unit = re.search(r"Ran (\d+) tests?\b", output)
    if unit:
        skipped = re.search(r"OK \(skipped=(\d+)\)", output)
        return int(unit[1]) - (int(skipped[1]) if skipped else 0)
    counts = re.findall(r"(?<![=\w])\b([1-9]\d*) passed\b", output)
    return max(map(int, counts), default=0)
