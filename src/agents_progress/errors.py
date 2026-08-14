"""Stable errors raised by the progress storage foundation."""


class ProgressError(Exception):
	"""Represent a user-facing progress error with a stable code."""

	code = "error"

	def __init__(self, message: str, details: dict[str, object] | None = None) -> None:
		self.message = message
		self.details = details or {}
		super().__init__(message)


class EmptyValueError(ProgressError):
	"""Indicate that a required text value was empty."""

	code = "empty"


class WrongObjectIdTypeError(ProgressError):
	"""Indicate that an object ID has the wrong type prefix."""

	code = "wrong-id-type"

	def __init__(self, expected_prefix: str, value: object) -> None:
		super().__init__(
			f"expected an object ID beginning with {expected_prefix!r}, got {value!r}",
			{"expected_prefix": expected_prefix, "value": value},
		)


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


class AlreadyInitialisedError(ProgressError):
	"""Indicate that the project has already been set up, so initialising it again would overwrite that."""

	code = "already-initialised"


class NotFoundError(ProgressError):
	"""Indicate that the requested record doesn't exist."""

	code = "not-found"


class InvalidStatusError(ProgressError):
	"""Indicate that a requested status filter is not one of the task's valid statuses."""

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
		cause: Exception,
	) -> None:
		message = (
			f"project binding recovery is required for {project_id}; "
			f"run: {recovery_command}"
		)
		super().__init__(
			message,
			{
				"project_id": project_id,
				"recovery_command": recovery_command,
				"cause": str(cause),
			},
		)
