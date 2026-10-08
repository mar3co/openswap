"""A stand-in for the Codex CLI used by the live-check tests. It is NOT Codex.

It implements just enough of the 0.157.1 surface the live check drives:
``--version``, ``mcp list``, ``features list``, ``sandbox`` (which runs the
command with **no** sandbox, so the sandbox gates must fail against it) and
``exec --json`` (reads the prompt on stdin, runs any numbered commands or
``sh ./helper.sh`` it asks for with plain ``/bin/sh``, and emits JSONL events).
"""

import json
import os
import re
import subprocess
import sys

DISABLED = ("apps", "hooks", "plugins", "multi_agent", "browser_use", "computer_use", "code_mode")


def emit(record):
    sys.stdout.write(json.dumps(record) + "\n")
    sys.stdout.flush()


def run_command(index, command):
    emit({"type": "item.started", "item": {"id": f"c{index}", "type": "command_execution",
                                           "command": command, "status": "in_progress"}})
    result = subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True)
    emit({"type": "item.completed", "item": {
        "id": f"c{index}", "type": "command_execution", "command": command,
        "aggregated_output": result.stdout + result.stderr, "exit_code": result.returncode,
        "status": "completed"}})


def main(argv):
    args = [a for a in argv if a != "--strict-config"]
    while "--disable" in args:
        i = args.index("--disable")
        del args[i:i + 2]
    if args == ["--version"]:
        print("codex-cli 0.157.1")
        return 0
    if args[:2] == ["mcp", "list"]:
        print("No MCP servers configured yet.")
        return 0
    if args[:2] == ["features", "list"]:
        print("shell_tool  stable  true")
        for name in DISABLED:
            print(f"{name}  stable  false")
        return 0
    if args[:1] == ["sandbox"]:
        command = args[args.index("/bin/sh"):]
        return subprocess.run(command, cwd=args[args.index("--cd") + 1]).returncode
    if args[:1] == ["login"]:
        return 0
    if args[:1] != ["exec"]:
        return 2
    result_path = args[args.index("--output-last-message") + 1]
    prompt = sys.stdin.read()
    emit({"type": "thread.started", "thread_id": "fake-thread"})
    emit({"type": "turn.started"})
    if "sh ./helper.sh" in prompt:
        run_command(0, "sh ./helper.sh")
        message = "done"
    else:
        commands = re.findall(r"^\d+\. (.+)$", prompt, flags=re.M)
        for index, command in enumerate(commands):
            run_command(index, command)
        if not commands:
            emit({"type": "item.completed", "item": {"id": "w", "type": "web_search", "query": "python"}})
        message = "DONE" if commands else "Python 3.x. Source: https://www.python.org/downloads/"
    emit({"type": "item.completed", "item": {"id": "m", "type": "agent_message", "text": message}})
    with open(result_path, "w") as handle:
        handle.write(message + "\n")
    emit({"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 7}})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
