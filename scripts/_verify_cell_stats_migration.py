"""Scratch-DB round-trip verification for the datasets/datafields cell-stats
normalization migration (s1c7e9a2d4b8).

SAFE: never touches the live DB. Creates a throwaway database, builds the OLD
(pre-refactor) schema + a few seed rows, runs `alembic upgrade head` then
`alembic downgrade -1` against it (via a subprocess with POSTGRES_DB overridden),
asserting the data migrates into the cell_stats tables on the way up and merges
back losslessly on the way down. Drops the scratch DB at the end.

Usage:  venv/Scripts/python.exe scripts/_verify_cell_stats_migration.py
"""
import os
import subprocess
import sys

import psycopg2

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRATCH = "alpha_gpt_migtest"


def _env():
    env = {}
    with open(os.path.join(_ROOT, ".env"), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


E = _env()
_HOST = E.get("POSTGRES_SERVER", "localhost")
_PORT = int(E.get("POSTGRES_PORT", "5433"))
_USER = E.get("POSTGRES_USER", "postgres")
_PWD = E.get("POSTGRES_PASSWORD", "")


def _conn(db, autocommit=False):
    c = psycopg2.connect(host=_HOST, port=_PORT, user=_USER, password=_PWD, dbname=db)
    c.autocommit = autocommit
    return c


def _admin_exec(sql):
    # CREATE/DROP DATABASE must run outside a transaction → connect to 'postgres'.
    c = _conn("postgres", autocommit=True)
    try:
        c.cursor().execute(sql)
    finally:
        c.close()


_OLD_DDL = """
CREATE TABLE datasets (
    id SERIAL PRIMARY KEY,
    dataset_id VARCHAR(100) NOT NULL,
    region VARCHAR(10) NOT NULL,
    universe VARCHAR(50) NOT NULL,
    name VARCHAR(200) NOT NULL DEFAULT '',
    description TEXT, category VARCHAR(100), subcategory VARCHAR(100),
    coverage DOUBLE PRECISION, value_score INTEGER, user_count INTEGER,
    alpha_count INTEGER, field_count INTEGER, pyramid_multiplier DOUBLE PRECISION,
    delay INTEGER DEFAULT 1, is_active BOOLEAN DEFAULT TRUE,
    mining_weight DOUBLE PRECISION DEFAULT 1.0, date_coverage DOUBLE PRECISION,
    themes JSONB, resources JSONB, last_synced_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT now(), updated_at TIMESTAMP DEFAULT now(),
    alpha_success_count INTEGER DEFAULT 0, alpha_fail_count INTEGER DEFAULT 0,
    CONSTRAINT uq_dataset_region_universe UNIQUE (dataset_id, region, universe)
);
CREATE TABLE datafields (
    id SERIAL PRIMARY KEY,
    dataset_id INTEGER REFERENCES datasets(id),
    region VARCHAR(10) NOT NULL, universe VARCHAR(50) NOT NULL, delay INTEGER DEFAULT 1,
    field_id VARCHAR(200) NOT NULL, field_name VARCHAR(200) NOT NULL,
    field_type VARCHAR(50), description TEXT,
    category VARCHAR(100), category_name VARCHAR(200),
    subcategory VARCHAR(100), subcategory_name VARCHAR(200),
    date_coverage DOUBLE PRECISION, coverage DOUBLE PRECISION,
    pyramid_multiplier DOUBLE PRECISION, user_count INTEGER, alpha_count INTEGER,
    themes JSONB, is_active BOOLEAN DEFAULT TRUE, created_at TIMESTAMP DEFAULT now(),
    CONSTRAINT uq_datafield_dataset_field UNIQUE (dataset_id, field_id)
);
CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY);
INSERT INTO alembic_version VALUES ('r9a1c5e3b7f2');

INSERT INTO datasets (dataset_id, region, universe, name, category, coverage,
    value_score, field_count, mining_weight, is_active, themes)
VALUES
 ('pv1', 'USA', 'TOP3000', 'Price Vol', 'pv', 0.91, 7, 2, 0.42, TRUE, '["t"]'::jsonb),
 ('fundamental6', 'USA', 'TOP3000', 'Fund 6', 'fundamental', 0.85, 9, 1, 0.77, TRUE, NULL);

INSERT INTO datafields (dataset_id, region, universe, delay, field_id, field_name,
    field_type, category, coverage, alpha_count, is_active)
VALUES
 (1, 'USA', 'TOP3000', 1, 'close', 'Close', 'MATRIX', 'pv', 0.99, 5, TRUE),
 (1, 'USA', 'TOP3000', 1, 'volume', 'Volume', 'MATRIX', 'pv', 0.98, 3, FALSE),
 (2, 'USA', 'TOP3000', 1, 'fn_assets', 'Assets', 'MATRIX', 'fundamental', 0.80, 1, TRUE);
"""


def _alembic(direction):
    """Run alembic upgrade head / downgrade -1 against the scratch DB."""
    env = dict(os.environ)
    env["POSTGRES_DB"] = _SCRATCH
    env["POSTGRES_SERVER"] = _HOST
    env["POSTGRES_PORT"] = str(_PORT)
    env["POSTGRES_USER"] = _USER
    env["POSTGRES_PASSWORD"] = _PWD
    args = ["upgrade", "head"] if direction == "up" else ["downgrade", "-1"]
    r = subprocess.run(
        [sys.executable, "-m", "alembic"] + args,
        cwd=os.path.join(_ROOT, "backend"), env=env,
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"[alembic {direction}] FAILED rc={r.returncode}")
        print(r.stdout[-2000:]); print(r.stderr[-2000:])
        raise SystemExit(1)
    print(f"[alembic {direction}] ok")


def _cols(cur, table):
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name=%s AND table_schema='public'", (table,))
    return {r[0] for r in cur.fetchall()}


def _tables(cur):
    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
    return {r[0] for r in cur.fetchall()}


def main():
    print(f"=== scratch DB round-trip: {_SCRATCH} ===")
    _admin_exec(f'DROP DATABASE IF EXISTS {_SCRATCH}')
    _admin_exec(f'CREATE DATABASE {_SCRATCH}')
    try:
        c = _conn(_SCRATCH); cur = c.cursor()
        cur.execute(_OLD_DDL); c.commit()
        print("seeded OLD schema: 2 datasets, 3 datafields")

        # ---------- UPGRADE ----------
        _alembic("up")
        tabs = _tables(cur)
        assert {"datasets", "datafields", "dataset_cell_stats", "datafield_cell_stats"} <= tabs, tabs
        ds_cols, df_cols = _cols(cur, "datasets"), _cols(cur, "datafields")
        assert "universe" not in ds_cols and "mining_weight" not in ds_cols, ds_cols
        assert "region" not in df_cols and "is_active" not in df_cols and "universe" not in df_cols, df_cols
        # UK swapped
        cur.execute("SELECT conname FROM pg_constraint WHERE conname='uq_dataset_region'")
        assert cur.fetchone(), "uq_dataset_region missing"
        cur.execute("SELECT conname FROM pg_constraint WHERE conname='uq_dataset_region_universe'")
        assert not cur.fetchone(), "old UK should be gone"
        # data moved
        cur.execute("SELECT count(*) FROM dataset_cell_stats"); assert cur.fetchone()[0] == 2
        cur.execute("SELECT count(*) FROM datafield_cell_stats"); assert cur.fetchone()[0] == 3
        cur.execute("""SELECT c.universe, c.delay, c.coverage, c.value_score, c.field_count,
                              c.mining_weight, c.is_active, c.themes
                       FROM dataset_cell_stats c JOIN datasets d ON c.dataset_ref=d.id
                       WHERE d.dataset_id='pv1'""")
        row = cur.fetchone()
        assert row == ('TOP3000', 1, 0.91, 7, 2, 0.42, True, ['t']), row
        # datafield is_active moved to cell (volume was FALSE)
        cur.execute("""SELECT c.is_active, c.coverage, c.alpha_count
                       FROM datafield_cell_stats c JOIN datafields f ON c.datafield_ref=f.id
                       WHERE f.field_id='volume'""")
        assert cur.fetchone() == (False, 0.98, 3)
        # FK datafields.dataset_id still resolves (ids unchanged)
        cur.execute("SELECT count(*) FROM datafields f JOIN datasets d ON f.dataset_id=d.id")
        assert cur.fetchone()[0] == 3
        print("UPGRADE asserts PASS (4 tables, cols dropped, UK swapped, values migrated, FK intact)")

        # ---------- MULTI-CELL: add a 2nd universe cell so downgrade's
        # "prefer TOP3000/delay=1" merge-back is actually exercised, and the
        # consumer-script SQL runs against a multi-cell migrated schema ----------
        cur.execute("INSERT INTO dataset_cell_stats (dataset_ref, universe, delay, coverage, mining_weight, is_active) "
                    "SELECT id, 'TOP1000', 1, 0.5, 0.33, true FROM datasets WHERE dataset_id='pv1'")
        cur.execute("INSERT INTO datafield_cell_stats (datafield_ref, universe, delay, is_active, coverage) "
                    "SELECT f.id, 'TOP1000', 1, true, 0.97 FROM datafields f JOIN datasets d ON f.dataset_id=d.id "
                    "WHERE d.dataset_id='pv1' AND f.field_id='close'")
        c.commit()

        # Consumer scripts fixed for the new schema must run without UndefinedColumn.
        # r7_audit query 1 (def + cell join):
        cur.execute("""
            SELECT d.region, dc.universe, d.dataset_id, COUNT(fc.id) AS field_count,
                   COUNT(*) FILTER (WHERE f.field_type='MATRIX' AND fc.id IS NOT NULL) AS matrix_count
            FROM datasets d
            JOIN dataset_cell_stats dc ON dc.dataset_ref = d.id AND dc.is_active = true
            LEFT JOIN datafields f ON f.dataset_id = d.id
            LEFT JOIN datafield_cell_stats fc ON fc.datafield_ref = f.id
                  AND fc.universe = dc.universe AND fc.delay = dc.delay AND fc.is_active = true
            GROUP BY d.region, dc.universe, d.dataset_id
        """)
        r7q1 = {(r[0], r[1], r[2]): r[3] for r in cur.fetchall()}
        # pv1 TOP3000 cell has 1 active field (close; volume inactive); TOP1000 has 1 (close).
        assert r7q1[('USA', 'TOP3000', 'pv1')] == 1, r7q1
        assert r7q1[('USA', 'TOP1000', 'pv1')] == 1, r7q1
        # r7_audit query 3 (USA TOP3000 field check):
        cur.execute("""
            SELECT DISTINCT f.field_id FROM datafields f
            JOIN datasets d ON f.dataset_id = d.id
            JOIN datafield_cell_stats fc ON fc.datafield_ref = f.id AND fc.universe = 'TOP3000'
            WHERE d.region = 'USA' AND f.field_id = ANY(%s)
        """, (['close', 'volume', 'fn_assets'],))
        assert {r[0] for r in cur.fetchall()} == {'close', 'volume', 'fn_assets'}
        # spike/ab cross-dataset join shape (df.region -> datasets join): must resolve.
        cur.execute("SELECT COUNT(DISTINCT d.id) FROM datafields df "
                    "LEFT JOIN datasets d ON d.id=df.dataset_id AND d.region='USA'")
        assert cur.fetchone()[0] == 2
        print("CONSUMER-SCRIPT SQL PASS (r7_audit + spike/ab join shapes run on migrated schema, no UndefinedColumn)")

        # ---------- DOWNGRADE ----------
        c.commit()
        _alembic("down")
        tabs = _tables(cur)
        assert "dataset_cell_stats" not in tabs and "datafield_cell_stats" not in tabs, tabs
        ds_cols, df_cols = _cols(cur, "datasets"), _cols(cur, "datafields")
        assert "universe" in ds_cols and "mining_weight" in ds_cols, ds_cols
        assert "region" in df_cols and "is_active" in df_cols, df_cols
        cur.execute("SELECT conname FROM pg_constraint WHERE conname='uq_dataset_region_universe'")
        assert cur.fetchone(), "old UK not restored"
        # values merged back — pv1 now had TWO cells (TOP3000 + TOP1000); the
        # downgrade must pick the TOP3000/delay=1 cell (0.91/0.42), NOT TOP1000
        # (0.5/0.33) → this assert proves the preference ordering.
        cur.execute("SELECT universe, coverage, value_score, field_count, mining_weight, is_active, themes FROM datasets WHERE dataset_id='pv1'")
        assert cur.fetchone() == ('TOP3000', 0.91, 7, 2, 0.42, True, ['t'])
        cur.execute("SELECT region, universe, is_active, coverage FROM datafields WHERE field_id='volume'")
        assert cur.fetchone() == ('USA', 'TOP3000', False, 0.98)
        cur.execute("SELECT count(*) FROM datasets"); assert cur.fetchone()[0] == 2
        cur.execute("SELECT count(*) FROM datafields"); assert cur.fetchone()[0] == 3
        print("DOWNGRADE asserts PASS (multi-cell→TOP3000-preferred merge-back, cols/UK restored, lossless)")

        c.close()
        print("\n=== ROUND-TRIP OK ===")
    finally:
        _admin_exec(f'DROP DATABASE IF EXISTS {_SCRATCH}')
        print(f"dropped scratch DB {_SCRATCH}")


if __name__ == "__main__":
    main()
