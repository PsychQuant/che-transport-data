"""DuckDB schema application + connection helpers for bus-eta-logger."""
import pathlib
import duckdb

SCHEMA_PATH = pathlib.Path(__file__).parent / "schema.sql"


def _statements(script: str):
    """Yield SQL statements, stripping `--` line comments first so semicolons
    inside comments don't corrupt the split."""
    no_comments = "\n".join(line.split("--", 1)[0] for line in script.splitlines())
    for stmt in no_comments.split(";"):
        s = stmt.strip()
        if s:
            yield s


def apply_schema(con: "duckdb.DuckDBPyConnection") -> None:
    """Apply the BCNF + SCD-2 schema. Idempotent (CREATE ... IF NOT EXISTS)."""
    for stmt in _statements(SCHEMA_PATH.read_text(encoding="utf-8")):
        con.execute(stmt)


def connect(path: str = ":memory:") -> "duckdb.DuckDBPyConnection":
    con = duckdb.connect(path)
    apply_schema(con)
    return con
