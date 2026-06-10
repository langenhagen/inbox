"""Thin wrapper around `opencode run --format json`."""

import json
import subprocess  # nosec  # trusted binary, not user input


def ask(prompt: str) -> str:
    """Run opencode with prompt and return the text response."""
    result = subprocess.run(  # nosec  # noqa: S603  # trusted binary in PATH
        [  # noqa: S607  # partial path intentional
            "opencode",
            "run",
            "--format",
            "json",
            prompt,
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    chunks: list[str] = []
    for line in result.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "text":
            continue
        part = event.get("part")
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, str) and text:
            chunks.append(text)
    return "".join(chunks).strip()
