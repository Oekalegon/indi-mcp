from pathlib import Path

from indi_mcp import db


def test_connect_creates_parent_directory_and_db_file(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "indi_mcp.sqlite3"

    with db.connect(db_path) as conn:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        conn.commit()

    assert db_path.exists()


def test_connect_reuses_the_same_file_across_calls(tmp_path: Path) -> None:
    db_path = tmp_path / "indi_mcp.sqlite3"

    with db.connect(db_path) as conn:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO t (value) VALUES ('a')")
        conn.commit()

    with db.connect(db_path) as conn:
        rows = conn.execute("SELECT value FROM t").fetchall()

    assert [row["value"] for row in rows] == ["a"]


def test_connect_row_factory_allows_column_access_by_name(tmp_path: Path) -> None:
    db_path = tmp_path / "indi_mcp.sqlite3"

    with db.connect(db_path) as conn:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO t (value) VALUES ('a')")
        conn.commit()
        row = conn.execute("SELECT * FROM t").fetchone()

    assert row["value"] == "a"


def test_connect_defaults_to_env_var_when_no_path_given(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "from-env.sqlite3"
    monkeypatch.setenv(db.DB_PATH_ENV, str(db_path))

    with db.connect() as conn:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        conn.commit()

    assert db_path.exists()
