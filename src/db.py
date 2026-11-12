"""Tiny SQLite data-access layer used by all pipeline stages."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

from src.paths import REPO_ROOT, ensure_parent


@contextmanager
def connect(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    """Open a connection with sane pragmas and commit on success."""
    path = ensure_parent(db_path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def run_sql_file(conn: sqlite3.Connection, relative_path: str) -> None:
    """Execute a multi-statement SQL file shipped in ``sql/``."""
    sql_path = REPO_ROOT / relative_path
    conn.executescript(sql_path.read_text(encoding="utf-8"))


def read_df(conn: sqlite3.Connection, sql: str, params: tuple | None = None) -> pd.DataFrame:
    return pd.read_sql_query(sql, conn, params=params or ())


def write_df(
    conn: sqlite3.Connection,
    df: pd.DataFrame,
    table: str,
    *,
    if_exists: str = "append",
    chunksize: int = 5_000,
) -> int:
    """Write a DataFrame, returning the row count for pipeline bookkeeping."""
    df.to_sql(table, conn, if_exists=if_exists, index=False, chunksize=chunksize, method="multi")
    return len(df)


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?", (table,)
    ).fetchone()
    return row is not None


def bulk_insert(conn: sqlite3.Connection, df: pd.DataFrame, table: str, columns: list[str]) -> int:
    """Fast ``executemany`` insert for wide tables (100k+ rows).

    ``DataFrame.to_sql`` is fine for small frames but spends most of its time
    building SQL strings; MES facts are written with a prepared statement.
    """
    placeholders = ",".join("?" for _ in columns)
    statement = f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})"
    conn.executemany(statement, df[columns].itertuples(index=False, name=None))
    return len(df)
