import os
import sqlite3
import threading

SQLITE_WRITE_LOCK = threading.RLock()


def open_sqlite_conn(db_path: str, timeout: float = 30.0) -> sqlite3.Connection:
    """Open a SQLite connection configured with WAL mode and 30s busy_timeout to prevent 'database is locked'."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=timeout, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=30000;")
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
    except sqlite3.OperationalError:
        pass
    return conn
