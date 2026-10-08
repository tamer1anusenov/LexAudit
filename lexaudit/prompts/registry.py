"""Load versioned prompt files from the prompts package."""
from __future__ import annotations

from pathlib import Path

from lexaudit.config.settings import get_settings

_PROMPTS_ROOT = Path(__file__).resolve().parent


class PromptNotFoundError(FileNotFoundError):
    """Raised when a prompt version or prompt file is unavailable."""


def list_versions() -> list[str]:
    """Return available prompt version directory names in sorted order."""
    return sorted(
        path.name
        for path in _PROMPTS_ROOT.iterdir()
        if path.is_dir() and not path.name.startswith((".", "__"))
    )


def load(version: str | None, name: str) -> str:
    """Read a Markdown prompt; ``None`` selects configured PROMPTS_VERSION."""
    selected_version = version or get_settings().PROMPTS_VERSION
    version_path = Path(selected_version)
    prompt_path = Path(name)
    if version_path.name != selected_version or prompt_path.name != name or prompt_path.suffix:
        raise ValueError("version must be a directory name and prompt name must be a simple filename stem")
    path = _PROMPTS_ROOT / version_path / f"{prompt_path.name}.md"
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PromptNotFoundError(f"prompt not found: {selected_version}/{prompt_path.name}.md") from exc
