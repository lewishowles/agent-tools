"""Open configured SQLite connections and short write transactions."""

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .errors import DatabaseBusyError
from .schema import is_busy_error, migrate

# Default database shared by friction CLI callers.
DEFAULT_DATABASE_PATH = Path.home() / ".agents" / "friction.db"
# Environment variable that selects a database without passing a CLI flag.
DATABASE_ENVIRONMENT_VARIABLE = "FRICTION_DATABASE"
# SQLite retry window for a concurrently held write lock.
BUSY_TIMEOUT_SECONDS = 5.0


def resolve_database_path(path: str | Path | None = None) -> Path:
	"""Resolve an explicit path, environment override, or the global default."""
	if path is not None:
		return Path(path).expanduser()

	environment_path = os.environ.get(DATABASE_ENVIRONMENT_VARIABLE)
	if environment_path:
		return Path(environment_path).expanduser()

	return DEFAULT_DATABASE_PATH


def _create_private_database_file(path: Path) -> None:
	"""Create a new database file with owner-only permissions when absent."""
	try:
		descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
	except FileExistsError:
		return

	os.close(descriptor)


def _busy_error() -> DatabaseBusyError:
	"""Build the stable error reported after the SQLite retry window expires."""
	return DatabaseBusyError(
		"database remained locked for the five-second retry window"
	)


def _configure_connection(connection: sqlite3.Connection) -> None:
	"""Apply the SQLite settings needed by concurrent CLI writers."""
	connection.row_factory = sqlite3.Row
	connection.execute("PRAGMA foreign_keys = ON")
	connection.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_SECONDS * 1000)}")
	try:
		connection.execute("PRAGMA journal_mode = WAL")
	except sqlite3.OperationalError as error:
		if is_busy_error(error):
			raise _busy_error() from error
		raise


def connect_database(path: str | Path | None = None) -> sqlite3.Connection:
	"""Open a migrated friction database at the resolved path."""
	database_path = resolve_database_path(path)
	database_path.parent.mkdir(parents=True, exist_ok=True)
	_create_private_database_file(database_path)

	try:
		connection = sqlite3.connect(
			database_path,
			timeout=BUSY_TIMEOUT_SECONDS,
			isolation_level=None,
		)
	except sqlite3.OperationalError as error:
		if is_busy_error(error):
			raise _busy_error() from error
		raise

	try:
		_configure_connection(connection)
		migrate(connection)
	except sqlite3.OperationalError as error:
		connection.close()
		if is_busy_error(error):
			raise _busy_error() from error
		raise
	except Exception:
		connection.close()
		raise

	return connection


class Database:
	"""Open friction connections against one configured SQLite file."""

	def __init__(self, path: str | Path | None = None) -> None:
		"""Store the path used by connections created through this instance."""
		self.path = resolve_database_path(path)

	def connect(self) -> sqlite3.Connection:
		"""Open and migrate a connection for the configured database."""
		return connect_database(self.path)

	@contextmanager
	def connection(self) -> Iterator[sqlite3.Connection]:
		"""Yield a connection and close it when the caller finishes."""
		connection = self.connect()
		try:
			yield connection
		finally:
			connection.close()

	@contextmanager
	def transaction(self) -> Iterator[sqlite3.Connection]:
		"""Yield a committed immediate transaction or roll it back on error."""
		with self.connection() as connection:
			try:
				connection.execute("BEGIN IMMEDIATE")
			except sqlite3.OperationalError as error:
				if is_busy_error(error):
					raise _busy_error() from error
				raise

			try:
				yield connection
			except sqlite3.OperationalError as error:
				if connection.in_transaction:
					connection.rollback()

				if is_busy_error(error):
					raise _busy_error() from error
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
						raise _busy_error() from error
					raise
