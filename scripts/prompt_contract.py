"""Shared dispatch-prompt contract for herdr-orchestrator scripts."""


def prepare_prompt(prompt: str, *, orchestrate: bool) -> str:
    if not orchestrate:
        return prompt
    if prompt.splitlines()[:1] == ["orchestrate"]:
        return prompt
    return f"orchestrate\n{prompt}"


if __name__ == "__main__":
    assert prepare_prompt("work", orchestrate=False) == "work"
    assert prepare_prompt("work", orchestrate=True) == "orchestrate\nwork"
    assert prepare_prompt("orchestrate\nwork", orchestrate=True) == "orchestrate\nwork"
