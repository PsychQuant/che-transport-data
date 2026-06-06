import duckdb
import db

EXPECTED_TABLES = {
    "dim_city", "dim_operator", "dim_route", "dim_stop", "dim_vehicle",
    "bridge_route_stop", "fact_arrival_event", "fact_eta_snapshot",
}

def _con():
    con = duckdb.connect()
    db.apply_schema(con)
    return con

def test_all_tables_created():
    con = _con()
    tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    assert EXPECTED_TABLES <= tables

def test_fact_tables_have_no_descriptive_name_columns():
    # BCNF thin-fact: facts store FKs + measures, never stop_name / route_name
    con = _con()
    for t in ("fact_arrival_event", "fact_eta_snapshot"):
        cols = [r[1].lower() for r in con.execute(f"PRAGMA table_info('{t}')").fetchall()]
        assert not any("name" in c for c in cols), f"{t} leaked a name column: {cols}"

def test_scd2_dims_have_validity_window():
    con = _con()
    for t in ("dim_route", "dim_stop", "dim_vehicle", "bridge_route_stop"):
        cols = {r[1].lower() for r in con.execute(f"PRAGMA table_info('{t}')").fetchall()}
        assert {"valid_from", "valid_to", "is_current"} <= cols, f"{t} missing SCD-2 cols: {cols}"

def test_type1_dims_are_not_versioned():
    con = _con()
    for t in ("dim_city", "dim_operator"):
        cols = {r[1].lower() for r in con.execute(f"PRAGMA table_info('{t}')").fetchall()}
        assert "valid_from" not in cols, f"{t} should be Type-1 (no validity window)"

def test_asof_join_resolves_version_current_at_event_time():
    # spec Example: dim_stop S123 version A [2026-01-01,2026-05-01) + B [2026-05-01,open);
    # arrival at 2026-03-10 must resolve to version A.
    con = _con()
    con.execute("""INSERT INTO dim_stop VALUES
      (1,'S123','Taipei','s1','站名A','NameA',25.0,121.5,'2026-01-01','2026-05-01',FALSE),
      (2,'S123','Taipei','s1','站名B','NameB',25.1,121.6,'2026-05-01',NULL,TRUE)""")
    con.execute("""INSERT INTO fact_arrival_event VALUES
      ('EAL-5200','R1',0,'S123',1,'2026-03-10 09:24:00',25.0,121.5,'2026-03-10 09:24:05','A2')""")
    row = con.execute("SELECT stop_name_zh FROM v_arrival_event_resolved WHERE plate='EAL-5200'").fetchone()
    assert row[0] == '站名A'

def test_asof_join_open_interval_for_current_version():
    con = _con()
    con.execute("""INSERT INTO dim_stop VALUES
      (1,'S999','Taipei','s9','現役','Cur',25.0,121.5,'2026-05-01',NULL,TRUE)""")
    con.execute("""INSERT INTO fact_arrival_event VALUES
      ('P1','R1',0,'S999',1,'2026-06-04 12:00:00',25.0,121.5,'2026-06-04 12:00:05','A2')""")
    row = con.execute("SELECT stop_name_zh FROM v_arrival_event_resolved WHERE plate='P1'").fetchone()
    assert row[0] == '現役'  # valid_to NULL = open interval, must match
