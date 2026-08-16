"""Configured SQLite connections and short write transactions."""

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .errors import DatabaseBusyError
from .schema import is_busy_error, migrate

# the one global database shared by every project, unless a path is given explicitly
DEFAULT_DATABASE_PATH = Path.home() / ".agents" / "progress.db"
# fallback database path, checked before DEFAULT_DATABASE_PATH when --database is omitted
DATABASE_ENVIRONMENT_VARIABLE = "AGENTS_PROGRESS_DATABASE"
# how long a connection waits on a locked database before raising DatabaseBusyError
BUSY_TIMEOUT_SECONDS = 5.0


def resolve_database_path(path: str | Path | None = None) -> Path:
	"""Resolve the configured global database path."""
	if path is not None:
		return Path(path).expanduser()

	environment_path = os.environ.get(DATABASE_ENVIRONMENT_VARIABLE)
	if environment_path:
		return Path(environment_path).expanduser()

	return DEFAULT_DATABASE_PATH


def _busy_error(error: sqlite3.OperationalError) -> DatabaseBusyError:
	"""Build the stable error raised when the busy timeout is exhausted."""
	return DatabaseBusyError(
		"database remained locked for the five-second retry window",
		{"retry_after_seconds": int(BUSY_TIMEOUT_SECONDS)},
	)


def _configure_connection(connection: sqlite3.Connection) -> None:
	"""Apply row access, foreign keys, busy timeout, and WAL mode to a connection."""
	connection.row_factory = sqlite3.Row
	connection.execute("PRAGMA foreign_keys = ON")
	connection.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_SECONDS * 1000)}")

	try:
		connection.execute("PRAGMA journal_mode = WAL")
	except sqlite3.OperationalError as error:
		if is_busy_error(error):
			raise _busy_error(error) from error
		raise


def connect_database(path: str | Path | None = None) -> sqlite3.Connection:
	"""Open, configure, and migrate a progress database connection."""
	database_path = resolve_database_path(path)
	database_path.parent.mkdir(parents=True, exist_ok=True)

	try:
		connection = sqlite3.connect(
			database_path,
			timeout=BUSY_TIMEOUT_SECONDS,
			isolation_level=None,
		)
	except sqlite3.OperationalError as error:
		if is_busy_error(error):
			raise _busy_error(error) from error
		raise

	try:
		_configure_connection(connection)
		migrate(connection)
	except Exception:
		connection.close()
		raise

	return connection


class Database:
	"""Open progress connections against one database path."""

	def __init__(self, path: str | Path | None = None) -> None:
		self.path = resolve_database_path(path)

	def connect(self) -> sqlite3.Connection:
		"""Open a configured connection and apply pending migrations."""
		return connect_database(self.path)

	@contextmanager
	def connection(self) -> Iterator[sqlite3.Connection]:
		"""Yield a migrated connection and close it afterwards."""
		connection = self.connect()
		try:
			yield connection
		finally:
			connection.close()

	@contextmanager
	def transaction(self) -> Iterator[sqlite3.Connection]:
		"""Run a short write transaction that begins before any mutation."""
		with self.connection() as connection:
			try:
				connection.execute("BEGIN IMMEDIATE")
			except sqlite3.OperationalError as error:
				if is_busy_error(error):
					raise _busy_error(error) from error
				raise

			try:
				yield connection
			except sqlite3.OperationalError as error:
				if connection.in_transaction:
					connection.rollback()

				if is_busy_error(error):
					raise _busy_error(error) from error
				raise
			except Exception:
				if connection.in_transaction:
					connection.rollback()
				raise
			else:
				try:
					connection.commit()
				except sqlite3.OperationalError as error:
					if is_busy_error(error):
						raise _busy_error(error) from error
					raise
