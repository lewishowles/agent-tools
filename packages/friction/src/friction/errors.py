"""Stable errors that callers can handle without SQLite implementation details."""


class DatabaseBusyError(RuntimeError):
	"""Indicate that the database remained locked beyond the retry window."""
