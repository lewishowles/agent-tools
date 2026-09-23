import sqlite3

import pytest

from agents_progress import database as database_module
from agents_progress.database import (
	DATABASE_ENVIRONMENT_VARIABLE,
	DEFAULT_DATABASE_PATH,
	Database,
	connect_database,
	resolve_database_path,
)
from agents_progress.errors import DatabaseBusyError


class _LockedConnection:
	def __init__(self, locked_statement: str | None = None) -> None:
		self.closed = False
		self.in_transaction = False
		self.locked_statement = locked_statement
		self.row_factory = None

	def execute(self, statement: str) -> "_LockedConnection":
		if statement == self.locked_statement:
			raise sqlite3.OperationalError("database is locked")

		if statement == "BEGIN IMMEDIATE":
			self.in_transaction = True

		return self

	def commit(self) -> None:
		if self.locked_statement == "COMMIT":
			raise sqlite3.OperationalError("database is locked")

		self.in_transaction = False

	def rollback(self) -> None:
		self.in_transaction = False

	def close(self) -> None:
		self.closed = True


def test_connect_database_converts_a_locked_connect_error(
	tmp_path, monkeypatch
) -> None:
	def locked_connect(*args, **kwargs):
		raise sqlite3.OperationalError("database is locked")

	monkeypatch.setattr(database_module.sqlite3, "connect", locked_connect)

	with pytest.raises(DatabaseBusyError):
		connect_database(tmp_path / "progress.db")


def test_connect_database_converts_a_locked_wal_configuration_error(
	tmp_path, monkeypatch
) -> None:
	connection = _LockedConnection("PRAGMA journal_mode = WAL")
	monkeypatch.setattr(
		database_module.sqlite3,
		"connect",
		lambda *args, **kwargs: connection,
	)
	monkeypatch.setattr(database_module, "migrate", lambda connection: None)

	with pytest.raises(DatabaseBusyError):
		connect_database(tmp_path / "progress.db")

	assert connection.closed


@pytest.mark.parametrize("locked_statement", ["BEGIN IMMEDIATE", "COMMIT"])
def test_transaction_converts_locked_begin_and_commit_errors(
	tmp_path, monkeypatch, locked_statement
) -> None:
	connection = _LockedConnection(locked_statement)
	database = Database(tmp_path / "progress.db")
	monkeypatch.setattr(database, "connect", lambda: connection)

	with pytest.raises(DatabaseBusyError):
		with database.transaction():
			pass

	assert connection.closed


def test_resolve_database_path_uses_the_default_without_overrides(monkeypatch) -> None:
	monkeypatch.delenv(DATABASE_ENVIRONMENT_VARIABLE, raising=False)

	assert resolve_database_path() == DEFAULT_DATABASE_PATH


def test_resolve_database_path_uses_the_environment_override(
	monkeypatch, tmp_path
) -> None:
	database_path = tmp_path / "environment.db"
	monkeypatch.setenv(DATABASE_ENVIRONMENT_VARIABLE, str(database_path))

	assert resolve_database_path() == database_path


def test_resolve_database_path_prefers_the_explicit_path_over_the_environment(
	monkeypatch, tmp_path
) -> None:
	monkeypatch.setenv(
		DATABASE_ENVIRONMENT_VARIABLE,
		str(tmp_path / "environment.db"),
	)
	explicit_path = tmp_path / "explicit.db"

	assert resolve_database_path(explicit_path) == explicit_path
