"""Generation and validation for human-readable progress object IDs."""

from collections.abc import Callable
import re
import secrets

from .errors import InvalidObjectIdError, ObjectIdCollisionError, WrongObjectIdTypeError

# type prefix each object ID starts with, one per domain entity
PROJECT_PREFIX = "prj_"
RELEASE_PREFIX = "rel_"
TASK_PREFIX = "tsk_"
CHUNK_PREFIX = "chk_"
NOTE_PREFIX = "nte_"

# the full set of recognised prefixes, used to validate and classify any object ID
OBJECT_PREFIXES = frozenset(
	{
		CHUNK_PREFIX,
		NOTE_PREFIX,
		PROJECT_PREFIX,
		RELEASE_PREFIX,
		TASK_PREFIX,
	}
)

# length of the random token appended after an object ID's prefix
_RANDOM_PART_LENGTH = 22
# characters secrets.token_urlsafe can produce, used to validate the random part
_URL_SAFE_RANDOM_PART = re.compile(r"[A-Za-z0-9_-]+")


def _require_known_prefix(prefix: str) -> str:
	"""Return a known object ID prefix, or raise if it isn't recognised."""
	if prefix not in OBJECT_PREFIXES:
		raise ValueError(f"unknown object ID prefix: {prefix!r}")

	return prefix


def validate_object_id(value: object, expected_prefix: str | None = None) -> str:
	"""Validate an object ID and return it unchanged."""
	if expected_prefix is not None:
		_require_known_prefix(expected_prefix)

	if not isinstance(value, str):
		raise InvalidObjectIdError(
			f"object ID must be text, got {type(value).__name__}"
		)

	if expected_prefix is not None and not value.startswith(expected_prefix):
		raise WrongObjectIdTypeError(expected_prefix, value)

	prefix = expected_prefix or next(
		(prefix for prefix in OBJECT_PREFIXES if value.startswith(prefix)),
		None,
	)

	if prefix is None:
		raise InvalidObjectIdError(f"unknown object ID prefix in {value!r}")

	random_part = value[len(prefix) :]

	if (
		len(random_part) < _RANDOM_PART_LENGTH
		or _URL_SAFE_RANDOM_PART.fullmatch(random_part) is None
	):
		raise InvalidObjectIdError(f"malformed object ID: {value!r}")

	return value


def is_valid_object_id(value: object, expected_prefix: str | None = None) -> bool:
	"""Return whether a value is a valid object ID of the expected type."""
	try:
		validate_object_id(value, expected_prefix)
	except (InvalidObjectIdError, WrongObjectIdTypeError):
		return False

	return True


def generate_object_id(
	prefix: str,
	exists: Callable[[str], bool] | None = None,
	max_attempts: int = 10,
	token_factory: Callable[[], str] | None = None,
) -> str:
	"""Generate a collision-resistant, type-prefixed object ID."""
	_require_known_prefix(prefix)

	if max_attempts < 1:
		raise ValueError("max_attempts must be at least 1")

	random_token = token_factory or (lambda: secrets.token_urlsafe(16))

	for _ in range(max_attempts):
		candidate = f"{prefix}{random_token()}"
		validate_object_id(candidate, prefix)

		if exists is None or not exists(candidate):
			return candidate

	raise ObjectIdCollisionError(
		f"could not generate a unique {prefix} object ID after {max_attempts} attempts",
		{"prefix": prefix, "attempts": max_attempts},
	)
