#!/usr/bin/env python3
"""Render and run bus ETA warehouse SQL scripts.

The SQL files are kept dependency-free. This helper supplies path/date/city
predicates and executes the schema plus the requested workload.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import tempfile
import time

import duckdb


HERE = pathlib.Path(__file__).resolve().parent
SCHEMA_SQL = HERE / "00_schema.sql"
LOAD_SQL = HERE / "10_bootstrap.sql"
SCD2_SQL = HERE / "30_scd2_patterns.sql"
VERIFY_SQL = HERE / "90_verify.sql"
COMPAT_SQL = HERE / "compat_probe.sql"


def sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def date_literal(value: str) -> str:
    # ISO date validation without importing datetime into SQL rendering logic.
    parts = value.split("-")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise SystemExit(f"invalid date: {value!r}; expected YYYY-MM-DD")
    return f"DATE {sql_quote(value)}"


def city_predicate(cities: list[str], column: str = "city") -> str:
    if not cities:
        return "TRUE"
    safe = []
    for city in cities:
        if not city.replace("_", "").isalnum():
            raise SystemExit(f"invalid city code: {city!r}")
        safe.append(sql_quote(city))
    return f"{column} IN ({', '.join(safe)})"


def render(sql: str, replacements: dict[str, str]) -> str:
    for key, value in replacements.items():
        sql = sql.replace("${" + key + "}", value)
    return sql


def predicates(args: argparse.Namespace) -> dict[str, str]:
    cities = [c.strip() for c in args.cities.split(",") if c.strip()]
    source_city = city_predicate(cities, "city")
    native_city = city_predicate(cities, "city")

    if args.mode == "incremental":
        if not args.load_date:
            raise SystemExit("--load-date is required for --mode incremental")
        day = date_literal(args.load_date)
        return {
            "DATE_FILTER": f"CAST(date AS DATE) = {day}",
            "FACT_DELETE_FILTER": f"service_date = {day} AND {native_city}",
            "CITY_FILTER": source_city,
            "LOAD_LABEL": f"incremental:{args.load_date}",
        }

    if args.mode in {"bootstrap", "verify"}:
        if args.load_date:
            day = date_literal(args.load_date)
            date_filter = f"CAST(date AS DATE) = {day}"
            fact_filter = f"service_date = {day} AND {native_city}"
            label = f"{args.mode}:{args.load_date}"
        elif args.start_date or args.end_date:
            clauses = []
            native_clauses = [native_city]
            if args.start_date:
                start = date_literal(args.start_date)
                clauses.append(f"CAST(date AS DATE) >= {start}")
                native_clauses.append(f"service_date >= {start}")
            if args.end_date:
                end = date_literal(args.end_date)
                clauses.append(f"CAST(date AS DATE) <= {end}")
                native_clauses.append(f"service_date <= {end}")
            date_filter = " AND ".join(clauses + [source_city])
            fact_filter = " AND ".join(native_clauses)
            label = f"{args.mode}:{args.start_date or 'begin'}:{args.end_date or 'end'}"
            return {
                "DATE_FILTER": date_filter,
                "FACT_DELETE_FILTER": fact_filter,
                "CITY_FILTER": "TRUE",
                "LOAD_LABEL": label,
            }
        else:
            return {
                "DATE_FILTER": source_city,
                "FACT_DELETE_FILTER": native_city,
                "CITY_FILTER": "TRUE",
                "LOAD_LABEL": f"{args.mode}:all",
            }
        return {
            "DATE_FILTER": date_filter,
            "FACT_DELETE_FILTER": fact_filter,
            "CITY_FILTER": source_city,
            "LOAD_LABEL": label,
        }

    return {
        "DATE_FILTER": "TRUE",
        "FACT_DELETE_FILTER": "TRUE",
        "CITY_FILTER": source_city,
        "LOAD_LABEL": args.mode,
    }


def run_sql(con: duckdb.DuckDBPyConnection, sql: str) -> None:
    result = con.execute(sql)
    if result.description:
        headers = [d[0] for d in result.description]
        print("\t".join(headers))
        for row in result.fetchall():
            print("\t".join("" if v is None else str(v) for v in row))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["bootstrap", "incremental", "verify", "scd2", "compat"], required=True)
    parser.add_argument("--db", required=True, help="DuckDB database path")
    parser.add_argument("--parquet-root", required=True, help="canonical bus-eta/parquet root")
    parser.add_argument("--cities", default="Taipei,NewTaipei", help="comma-separated city codes")
    parser.add_argument("--load-date", help="single YYYY-MM-DD partition")
    parser.add_argument("--start-date", help="inclusive YYYY-MM-DD")
    parser.add_argument("--end-date", help="inclusive YYYY-MM-DD")
    parser.add_argument("--scd-valid-from", help="TIMESTAMPTZ literal value for SCD2 changes")
    parser.add_argument("--tmp-dir", default=tempfile.gettempdir())
    parser.add_argument("--print-sql", action="store_true")
    args = parser.parse_args(argv)

    replacements = predicates(args)
    replacements.update(
        {
            "PARQUET_ROOT": args.parquet_root.rstrip("/").replace("'", "''"),
            "TMP_DIR": args.tmp_dir.rstrip("/").replace("'", "''"),
            "SCD_VALID_FROM_EXPR": (
                f"TIMESTAMPTZ {sql_quote(args.scd_valid_from)}"
                if args.scd_valid_from
                else "current_timestamp"
            ),
        }
    )

    if args.mode == "compat":
        scripts = [COMPAT_SQL]
    elif args.mode == "verify":
        scripts = [SCHEMA_SQL, VERIFY_SQL]
    elif args.mode == "scd2":
        scripts = [SCHEMA_SQL, SCD2_SQL]
    else:
        scripts = [SCHEMA_SQL, LOAD_SQL]

    con = duckdb.connect(args.db)
    print(f"duckdb_version\t{duckdb.__version__}")
    started = time.monotonic()
    for path in scripts:
        sql = render(path.read_text(encoding="utf-8"), replacements)
        if args.print_sql:
            print(f"-- rendered: {path}")
            print(sql)
        run_sql(con, sql)
    elapsed = time.monotonic() - started
    print(f"elapsed_sec\t{elapsed:.3f}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
