"""Shared fixtures for agent-run tests."""

import pytest


@pytest.fixture(autouse=True)
def use_deterministic_text_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Print plain text 80 columns wide, so expected output matches on every machine."""
    monkeypatch.setattr(
        "agent_run.output._RENDER_OPTIONS",
        {"plain": True, "width": 80, "raise_on_missing": False},
    )
