"""Read optional, repository-local review packet settings."""

import tomllib
from dataclasses import dataclass
from pathlib import Path

# The settings file name, looked up at the repository root.
SETTINGS_NAME = ".review-packet.toml"


class InvalidSettingsError(ValueError):
	"""The settings file cannot be parsed or has invalid values."""


@dataclass(frozen=True)
class Settings:
	"""What the repository asks the review packet to run and include."""

	# Registered agent-run commands for a later build step to run.
	checks: tuple[str, ...]
	# Extra repository files to add to the packet.
	include: tuple[str, ...]
	# Whether the repository has a settings file.
	found: bool


def read_settings(root: Path) -> Settings:
	"""Read the repository's settings file, if it has one.

	Args:
		root: The repository worktree root.

	Returns:
		The configured checks and extra files, or empty settings when the
		file does not exist.

	Raises:
		InvalidSettingsError: The file is not valid TOML, or checks or include
			is not a list of strings.
		OSError: The file exists but cannot be read.
	"""
	# The settings file at the repository root.
	path = root / SETTINGS_NAME

	if not path.exists():
		return Settings((), (), False)

	try:
		with path.open("rb") as source:
			# The parsed TOML table.
			data = tomllib.load(source)
	except tomllib.TOMLDecodeError as error:
		raise InvalidSettingsError(f"Invalid settings: {error}") from error

	# The configured check names, before validation.
	checks = data.get("checks", [])
	# The configured extra files, before validation.
	include = data.get("include", [])
	# Any misspelt or unsupported setting names.
	unknown = set(data) - {"checks", "include"}

	if unknown:
		raise InvalidSettingsError(f"Unknown settings: {', '.join(sorted(unknown))}.")

	if not all(_is_string_list(value) for value in (checks, include)):
		raise InvalidSettingsError(
			"Settings checks and include must be lists of strings."
		)

	return Settings(tuple(checks), tuple(include), True)


def _is_string_list(value: object) -> bool:
	"""Check that a settings value is a list of strings.

	Args:
		value: One value from the settings file.

	Returns:
		True when the value is a list and every item is a string.
	"""
	return isinstance(value, list) and all(isinstance(item, str) for item in value)
