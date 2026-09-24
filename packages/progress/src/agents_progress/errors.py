"""Stable errors raised by the progress storage foundation."""

# Error codes are stable identifiers used by the CLI's text and `--json` output.

# Names used when an ID has a valid prefix for the wrong object type.
_OBJECT_TYPE_NAMES = {
	"tsk_": "task",
	"chk_": "chunk",
	"rel_": "release",
	"prj_": "project",
	"nte_": "note",
}


class ProgressError(Exception):
	"""Represent a user-facing progress error with a stable code."""

	code = "error"

	def __init__(self, message: str, details: dict[str, object] | None = None) -> None:
		"""Store the error message and optional structured details."""
		self.message = message
		self.details = details or {}
		super().__init__(message)


class EmptyValueError(ProgressError):
	"""Indicate that a required text value was empty."""

	code = "empty"


class WrongObjectIdTypeError(ProgressError):
	"""Indicate that an object ID has the wrong type prefix."""

	code = "wrong-id-type"

	def __init__(
		self,
		expected_prefixes: str | tuple[str, ...],
		value: object,
	) -> None:
		"""Build the message and details from the accepted object ID prefixes."""
		if isinstance(expected_prefixes, tuple):
			prefixes = list(expected_prefixes)
		else:
			prefixes = [expected_prefixes]

		type_names = " or ".join(
			f"{_OBJECT_TYPE_NAMES[prefix]} ({prefix})" for prefix in prefixes
		)
		message = f"expected a {type_names} ID, got {value!r}"
		details: dict[str, object] = {"expected_prefixes": prefixes, "value": value}
		if len(prefixes) == 1:
			details["expected_prefix"] = prefixes[0]

		super().__init__(message, details)


class InvalidObjectIdError(ProgressError):
	"""Indicate that an object ID isn't in the right format."""

	code = "invalid-id"


class ObjectIdCollisionError(ProgressError):
	"""Indicate that generating a unique ID failed after using up every retry attempt."""

	code = "id-collision"


class DatabaseBusyError(ProgressError):
	"""Indicate that the database stayed locked past the configured timeout."""

	code = "database-busy"


class MigrationFailedError(ProgressError):
	"""Indicate that a schema migration failed."""

	code = "migration-failed"


class StaleSchemaError(ProgressError):
	"""Indicate that the database schema is newer than this package."""

	code = "stale-schema"


class NotAProjectError(ProgressError):
	"""Indicate that the requested path is not inside a Git repository."""

	code = "not-a-project"


class UninitialisedProjectError(ProgressError):
	"""Indicate that a Git repository has no progress project binding."""

	code = "uninitialised-project"


class OrphanedProjectError(ProgressError):
	"""Indicate that a Git binding points to a missing database row."""

	code = "orphaned-project"


class NotFoundError(ProgressError):
	"""Indicate that the requested record doesn't exist."""

	code = "not-found"


class StillReferencedError(ProgressError):
	"""Indicate that a record cannot be removed while other rows reference it."""

	code = "still-referenced"


class InvalidDependencyError(ProgressError):
	"""Indicate that a task dependency cannot be added."""

	code = "invalid-dependency"


class DuplicateDependencyError(ProgressError):
	"""Indicate that a task dependency already exists."""

	code = "duplicate-dependency"


class AlreadyExistsError(ProgressError):
	"""Indicate that a unique record already exists."""

	code = "already-exists"


class UnresolvedDependenciesError(ProgressError):
	"""Indicate that a task cannot start until its dependencies are done."""

	code = "unresolved-dependencies"


class PendingChunksError(ProgressError):
	"""Indicate that a task still has chunks to complete."""

	code = "pending-chunks"


class InvalidTransitionError(ProgressError):
	"""Indicate that a lifecycle transition is not valid for the current state."""

	code = "invalid-transition"


class InvalidStatusError(ProgressError):
	"""Indicate that a requested status is not valid for the command."""

	code = "invalid-status"


class GitBindingError(ProgressError):
	"""Indicate that Git could not read or write the local link between this repository and its project."""

	code = "git-binding-failed"


class ProjectBindingRecoveryError(ProgressError):
	"""Indicate that a failed binding operation also defeated compensation."""

	code = "binding-recovery-required"

	def __init__(
		self,
		project_id: str,
		recovery_command: str,
		write_error: Exception,
		rollback_error: Exception,
	) -> None:
		"""Create an error with instructions for recovering a failed project binding."""
		message = (
			f"project binding recovery is required for {project_id}; "
			f"binding write failed: {write_error}; "
			f"rollback failed: {rollback_error}; "
			f"run: {recovery_command}"
		)
		super().__init__(
			message,
			{
				"project_id": project_id,
				"recovery_command": recovery_command,
				"write_error": str(write_error),
				"rollback_error": str(rollback_error),
			},
		)
