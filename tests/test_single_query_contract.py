"""Tests for SINGLE_QUERY fallback contract discovery."""

from src.code_rewriter.tools.pipeline_analysis import build_contracts_from_intent

DB_APP = '''\
class DatabaseClient:
    QUERY_1 = """SELECT COUNT(*) FROM badges;"""
    QUERY_2 = """SELECT COUNT(*) FROM users;"""
    QUERY_3 = """SELECT COUNT(*) FROM posts;"""

    def run_query1(self):
        with self._conn.cursor() as cur:
            cur.execute(self.QUERY_1)
            return cur.fetchall()

    def run_query2(self):
        with self._conn.cursor() as cur:
            cur.execute(self.QUERY_2)
            return cur.fetchall()

    def run_query3(self):
        with self._conn.cursor() as cur:
            cur.execute(self.QUERY_3)
            return cur.fetchall()


def main():
    client = DatabaseClient()
    client.run_query1()
    client.run_query2()
    client.run_query3()
'''

NON_QUERY_APP = '''\
class DatabaseClient:
    def run_dynamic(self, sql):
        with self._conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()
'''

LOOP_APP = '''\
def get_users(ids):
    for user_id in ids:
        cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
'''


def _write_target(tmp_path, text):
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    (target_dir / "db_app.py").write_text(text)
    return str(target_dir)


def _intent():
    return {"optimization_targets": [{"file": "db_app.py"}]}


def test_single_query_fallback_discovers_qualifiers(tmp_path):
    target_dir = _write_target(tmp_path, DB_APP)

    analyses, contracts = build_contracts_from_intent(target_dir, _intent())

    assert len(contracts) == 3
    assert all(c.pattern == "SINGLE_QUERY" for c in contracts)
    assert [c.target.function for c in contracts] == [
        "run_query1",
        "run_query2",
        "run_query3",
    ]
    assert [c.allowed_regions for c in contracts] == [
        ["DatabaseClient.run_query1"],
        ["DatabaseClient.run_query2"],
        ["DatabaseClient.run_query3"],
    ]
    assert all("main" not in c.allowed_regions for c in contracts)
    assert set(analyses) == {"db_app.py"}


def test_single_query_fallback_ignores_non_query_constant(tmp_path):
    target_dir = _write_target(tmp_path, NON_QUERY_APP)

    _analyses, contracts = build_contracts_from_intent(target_dir, _intent())

    assert contracts == []


def test_single_query_fallback_does_not_fire_with_loop_target(tmp_path):
    target_dir = _write_target(tmp_path, LOOP_APP)

    _analyses, contracts = build_contracts_from_intent(target_dir, _intent())

    assert len(contracts) == 1
    assert contracts[0].pattern == "N_PLUS_ONE_QUERY"
    assert contracts[0].target.function == "get_users"
