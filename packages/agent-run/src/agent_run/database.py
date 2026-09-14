"""Open configured SQLite connections and apply schema migrations."""

import os
import sqlite3
from pathlib import Path

from .schema import migrate

# Database used when neither a path nor the environment variable is given.
DEFAULT_DATABASE_PATH = Path.home() / ".agents" / "agent-run.db"
# Environment variable that points agent-run at a different database file.
DATABASE_ENVIRONMENT_VARIABLE = "AGENT_RUN_DATABASE"


def resolve_database_path(path: str | Path | None = None) -> Path:
    """Return the database path: the given path, then AGENT_RUN_DATABASE, then the default."""
    if path is not None:
        return Path(path).expanduser()

    environment_path = os.environ.get(DATABASE_ENVIRONMENT_VARIABLE)
    if environment_path:
        return Path(environment_path).expanduser()

    return DEFAULT_DATABASE_PATH


def _create_private_database_file(path: Path) -> None:
    """Create the database file readable only by its owner, leaving an existing file alone.

    SQLite would otherwise create the file with the default umask, which usually
    lets other users on the machine read stored commands.
    """
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return

    os.close(descriptor)


def connect_database(path: str | Path | None = None) -> sqlite3.Connection:
    """Open the database, creating it if needed and applying pending migrations.

    The caller must close the returned connection. It runs in autocommit mode, so
    callers start their own transactions when a change spans several statements.
    """
    database_path = resolve_database_path(path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    _create_private_database_file(database_path)

    connection = sqlite3.connect(database_path, isolation_level=None)
    try:
        migrate(connection)
    except Exception:
        connection.close()
        raise

    return connection
