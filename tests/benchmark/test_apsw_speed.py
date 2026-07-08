"""Benchmark: apsw should be faster than sqlite3 for bulk operations."""

import tempfile
import time


def test_apsw_vs_sqlite3_speed() -> None:
    """apsw should be faster for bulk insert/query operations."""
    import sqlite3 as std_sqlite3

    import apsw

    n_records = 10000

    # --- stdlib sqlite3 ---
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        std_path = f.name
    conn = std_sqlite3.connect(std_path)
    conn.execute("CREATE TABLE t (id INT, val TEXT)")
    t0 = time.perf_counter()
    for i in range(n_records):
        conn.execute("INSERT INTO t VALUES (?, ?)", (i, f"val_{i}"))
    conn.commit()
    cursor = conn.execute("SELECT COUNT(*) FROM t")
    std_count = cursor.fetchone()[0]
    t_std = time.perf_counter() - t0
    conn.close()

    # --- apsw (with explicit transaction) ---
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        apsw_path = f.name
    conn = apsw.Connection(apsw_path)
    conn.execute("CREATE TABLE t (id INT, val TEXT)")
    t0 = time.perf_counter()
    conn.execute("BEGIN")
    for i in range(n_records):
        conn.execute("INSERT INTO t VALUES (?, ?)", (i, f"val_{i}"))
    conn.execute("COMMIT")
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM t")
    apsw_count = list(cursor)[0][0]
    t_apsw = time.perf_counter() - t0
    conn.close()

    assert std_count == n_records
    assert apsw_count == n_records
    assert t_apsw < t_std * 1.2  # should be comparable or faster
