"""Storage foundation for the progress CLI."""

# Single source of truth for the package version. hatchling reads this literal at
# build time (see [tool.hatch.version] in pyproject.toml), and the CLI reports it
# for `progress --version`, so keep it a plain string assignment.
__version__ = "0.2.0"

from .database import (
	BUSY_TIMEOUT_SECONDS,
	DATABASE_ENVIRONMENT_VARIABLE,
	DEFAULT_DATABASE_PATH,
	Database,
	connect_database,
	resolve_database_path,
)
from .errors import (
	AlreadyExistsError,
	DatabaseBusyError,
	DuplicateDependencyError,
	EmptyValueError,
	GitBindingError,
	InvalidDependencyError,
	InvalidObjectIdError,
	InvalidStatusError,
	InvalidTransitionError,
	MigrationFailedError,
	NotAProjectError,
	NotFoundError,
	ObjectIdCollisionError,
	OrphanedProjectError,
	PendingChunksError,
	ProgressError,
	ProjectBindingRecoveryError,
	StaleSchemaError,
	UninitialisedProjectError,
	UnresolvedDependenciesError,
	WrongObjectIdTypeError,
)
from .ids import (
	CHUNK_PREFIX,
	NOTE_PREFIX,
	PROJECT_PREFIX,
	RELEASE_PREFIX,
	TASK_PREFIX,
	generate_object_id,
	is_valid_object_id,
	validate_object_id,
)
from .models import Chunk, Context, Note, Release, Task
from .projects import Project, ProjectStore
from .reads import ReadStore
from .writes import WriteStore

__all__ = [
	"BUSY_TIMEOUT_SECONDS",
	"CHUNK_PREFIX",
	"DATABASE_ENVIRONMENT_VARIABLE",
	"DEFAULT_DATABASE_PATH",
	"NOTE_PREFIX",
	"PROJECT_PREFIX",
	"RELEASE_PREFIX",
	"TASK_PREFIX",
	"AlreadyExistsError",
	"Chunk",
	"Context",
	"Database",
	"DatabaseBusyError",
	"DuplicateDependencyError",
	"EmptyValueError",
	"GitBindingError",
	"InvalidDependencyError",
	"InvalidObjectIdError",
	"InvalidStatusError",
	"InvalidTransitionError",
	"MigrationFailedError",
	"NotAProjectError",
	"NotFoundError",
	"Note",
	"ObjectIdCollisionError",
	"OrphanedProjectError",
	"PendingChunksError",
	"ProgressError",
	"Project",
	"ProjectBindingRecoveryError",
	"ProjectStore",
	"ReadStore",
	"Release",
	"StaleSchemaError",
	"Task",
	"UninitialisedProjectError",
	"UnresolvedDependenciesError",
	"WriteStore",
	"WrongObjectIdTypeError",
	"__version__",
	"connect_database",
	"generate_object_id",
	"is_valid_object_id",
	"resolve_database_path",
	"validate_object_id",
]
