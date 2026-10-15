"""SINGLE_QUERY workflow behaviors: attempt budget, constant-aware change, planner."""

import asyncio
import inspect

from src.code_rewriter.models.rewrite_models import RewriteContract, RewriteTarget
from src.code_rewriter.tools import planner
from src.code_rewriter.workflow import (
    MAX_ATTEMPTS_PER_TARGET,
    MAX_ATTEMPTS_SINGLE_QUERY,
    _target_unchanged,
    make_orchestrate,
)

_SINGLE_QUERY_SRC = (
    "class Repo:\n"
    '    QUERY_1 = "SELECT * FROM t WHERE id = %s"\n'
    "\n"
    "    def get(self, cur, id):\n"
    "        cur.execute(self.QUERY_1)\n"
    "        return cur.fetchall()\n"
)


class _FakeAsyncContext:
    def __init__(self, state: dict | None = None) -> None:
        self.state = state if state is not None else {}

    async def run_node(self, node, node_input=None):
        result = node(self, node_input)
        if inspect.isawaitable(result):
            result = await result
        return result


async def _drive(orchestrate, ctx):
    events = []
    async for event in orchestrate(ctx):
        events.append(event)
    return events


def _run_budget(tmp_path, pattern):
    target_dir = tmp_path / f"target-{pattern}"
    sandbox = tmp_path / f"sandbox-{pattern}"
    target_dir.mkdir()
    sandbox.mkdir()
    (target_dir / "repo.py").write_text("original\n")
    (sandbox / "repo.py").write_text("original\n")

    optimizer_calls = {"n": 0}

    def optimizer(ctx, node_input=None):
        optimizer_calls["n"] += 1
        (sandbox / "repo.py").write_text(f"attempt-{optimizer_calls['n']}\n")

    verify_calls = {"n": 0}

    def verify(ctx, node_input=None):
        verify_calls["n"] += 1
        return {
            "status": "FAIL",
            "violations": [
                {
                    "code": f"E{verify_calls['n']}",
                    "severity": "ERROR",
                    "message": str(verify_calls["n"]),
                }
            ],
        }

    ctx = _FakeAsyncContext({
        "target": str(target_dir),
        "sandbox": str(sandbox),
        "rewrite_contracts": [
            {
                "target": {"file": "repo.py", "qualified_function": "get_users"},
                "pattern": pattern,
            }
        ],
    })
    orchestrate = make_orchestrate(optimizer, verify)
    asyncio.run(_drive(orchestrate, ctx))
    return optimizer_calls["n"]


def test_single_query_uses_two_attempts(tmp_path):
    assert MAX_ATTEMPTS_SINGLE_QUERY == 2
    assert _run_budget(tmp_path, "SINGLE_QUERY") == 2


def test_normal_pattern_uses_five_attempts(tmp_path):
    assert _run_budget(tmp_path, "N_PLUS_ONE_QUERY") == MAX_ATTEMPTS_PER_TARGET


def test_target_unchanged_detects_constant_edit(tmp_path):
    target_dir = tmp_path / "target"
    sandbox = tmp_path / "sandbox"
    target_dir.mkdir()
    sandbox.mkdir()
    (target_dir / "repo.py").write_text(_SINGLE_QUERY_SRC)
    (sandbox / "repo.py").write_text(
        _SINGLE_QUERY_SRC.replace("WHERE id = %s", "WHERE id IN (%s)")
    )

    assert (
        _target_unchanged(
            str(target_dir), str(sandbox), "repo.py", "Repo.get", "SINGLE_QUERY"
        )
        is False
    )


def test_target_unchanged_when_constant_same(tmp_path):
    target_dir = tmp_path / "target"
    sandbox = tmp_path / "sandbox"
    target_dir.mkdir()
    sandbox.mkdir()
    (target_dir / "repo.py").write_text(_SINGLE_QUERY_SRC)
    (sandbox / "repo.py").write_text(_SINGLE_QUERY_SRC)

    assert (
        _target_unchanged(
            str(target_dir), str(sandbox), "repo.py", "Repo.get", "SINGLE_QUERY"
        )
        is True
    )


def test_planner_preserves_single_query_pattern(monkeypatch, tmp_path):
    (tmp_path / "app.py").write_text(_SINGLE_QUERY_SRC)

    matched = RewriteContract(
        rewrite_id="r1",
        target=RewriteTarget(file="app.py", function="get", qualified_function="Repo.get"),
        pattern="N_PLUS_ONE_QUERY",
        strategy="COMBINING_QUERIES",
        targets=[RewriteTarget(file="app.py", function="get", qualified_function="Repo.get")],
    )
    other = RewriteContract(
        rewrite_id="r2",
        target=RewriteTarget(file="app.py", function="other", qualified_function="Repo.other"),
        pattern="N_PLUS_ONE_QUERY",
        strategy="COMBINING_QUERIES",
        targets=[RewriteTarget(file="app.py", function="other", qualified_function="Repo.other")],
    )

    monkeypatch.setattr(
        planner,
        "_maybe_parse",
        lambda _value: {"optimization_targets": [{"file": "app.py", "description": "d"}]},
    )
    monkeypatch.setattr(
        planner, "build_contracts_from_intent", lambda *a, **k: ({}, [matched, other])
    )
    monkeypatch.setattr(planner, "build_target_context_map", lambda *a, **k: {})

    class _Ctx:
        def __init__(self):
            self.state = {
                "target": str(tmp_path),
                "intent_extractor_output": "{}",
                "rewrite_contracts": [
                    {
                        "rewrite_id": "old",
                        "pattern": "SINGLE_QUERY",
                        "strategy": "PREDICATE_PUSHDOWN",
                        "target": {"file": "app.py", "qualified_function": "Repo.get"},
                    }
                ],
            }

    ctx = _Ctx()
    planner.get_optimization_strategies(ctx)
    contracts = {c["rewrite_id"]: c for c in ctx.state["rewrite_contracts"]}

    assert contracts["r1"]["pattern"] == "SINGLE_QUERY"
    assert contracts["r1"]["strategy"] == "PREDICATE_PUSHDOWN"
    assert contracts["r2"]["pattern"] == "N_PLUS_ONE_QUERY"
    assert contracts["r2"]["strategy"] == "COMBINING_QUERIES"
