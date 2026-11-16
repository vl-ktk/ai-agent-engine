"""Offline demonstration. Only writes inside an automatically deleted temp folder."""

from __future__ import annotations

from tempfile import TemporaryDirectory
from pathlib import Path

from .core import Action, Agent, ModelTurn, ScriptedModel
from .tools import SandboxFiles


def main() -> None:
    with TemporaryDirectory(prefix="ai-agent-engine-demo-") as directory:
        sandbox = SandboxFiles(Path(directory))
        model = ScriptedModel((
            ModelTurn(action=Action("write_new_file", {"path": "hello.txt", "content": "Agent test passed"})),
            ModelTurn(action=Action("read_file", {"path": "hello.txt"})),
            ModelTurn(final_text="Offline agent completed a safe write-and-read task."),
        ))
        result = Agent(model=model, tools=sandbox.allowed_tools()).run("Create and check a demo file")
        print(f"status={result.status}; steps={result.steps}; answer={result.answer}")
        print("audit=" + ", ".join(f"{event.event}:{event.tool or '-'}" for event in result.audit))
        if result.status != "done":
            raise SystemExit(1)


if __name__ == "__main__":
    main()
