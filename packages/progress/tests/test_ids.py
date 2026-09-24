import pytest

from agents_progress.errors import (
	InvalidObjectIdError,
	ObjectIdCollisionError,
	WrongObjectIdTypeError,
)
from agents_progress.ids import (
	CHUNK_PREFIX,
	NOTE_PREFIX,
	PROJECT_PREFIX,
	RELEASE_PREFIX,
	TASK_PREFIX,
	generate_object_id,
	object_id_prefix,
	validate_object_id,
)


@pytest.mark.parametrize(
	"prefix",
	[CHUNK_PREFIX, NOTE_PREFIX, PROJECT_PREFIX, RELEASE_PREFIX, TASK_PREFIX],
)
def test_generated_ids_have_a_type_prefix_and_128_bits_of_random_input(
	prefix: str,
) -> None:
	object_id = generate_object_id(prefix)

	assert object_id.startswith(prefix)
	assert len(object_id.removeprefix(prefix)) >= 22
	assert validate_object_id(object_id, prefix) == object_id


def test_validation_rejects_a_wrong_prefix_before_lookup() -> None:
	with pytest.raises(
		WrongObjectIdTypeError,
		match=r"expected a project \(prj_\) ID, got 'tsk_aaaaaaaaaaaaaaaaaaaaaa'",
	):
		validate_object_id("tsk_" + "a" * 22, PROJECT_PREFIX)


def test_wrong_id_type_error_keeps_the_single_prefix_detail() -> None:
	"""Keep the original single-prefix detail while listing all accepted prefixes."""
	error = WrongObjectIdTypeError("tsk_", "rel_" + "a" * 22)

	assert error.details == {
		"expected_prefix": "tsk_",
		"expected_prefixes": ["tsk_"],
		"value": "rel_" + "a" * 22,
	}


@pytest.mark.parametrize(
	"prefix",
	[CHUNK_PREFIX, NOTE_PREFIX, PROJECT_PREFIX, RELEASE_PREFIX, TASK_PREFIX],
)
def test_object_id_prefix_recognises_every_object_type(prefix: str) -> None:
	assert object_id_prefix(prefix + "a" * 22) == prefix


@pytest.mark.parametrize(
	"value",
	[None, "", "a" * 22, "other_" + "a" * 22, "tsk_short", "tsk_" + "a" * 21 + "="],
)
def test_object_id_prefix_rejects_invalid_ids(value: object) -> None:
	with pytest.raises(InvalidObjectIdError):
		object_id_prefix(value)


def test_validation_rejects_malformed_random_parts() -> None:
	with pytest.raises(InvalidObjectIdError):
		validate_object_id(PROJECT_PREFIX + "too-short", PROJECT_PREFIX)

	with pytest.raises(InvalidObjectIdError):
		validate_object_id(PROJECT_PREFIX + "a" * 21 + "=", PROJECT_PREFIX)


def test_generation_retries_a_collision() -> None:
	tokens = iter(["a" * 22, "b" * 22])
	first_id = PROJECT_PREFIX + "a" * 22

	object_id = generate_object_id(
		PROJECT_PREFIX,
		exists=lambda candidate: candidate == first_id,
		token_factory=lambda: next(tokens),
	)

	assert object_id == PROJECT_PREFIX + "b" * 22


def test_generation_reports_exhausted_collisions() -> None:
	with pytest.raises(ObjectIdCollisionError):
		generate_object_id(
			PROJECT_PREFIX,
			exists=lambda candidate: True,
			max_attempts=2,
			token_factory=lambda: "a" * 22,
		)
