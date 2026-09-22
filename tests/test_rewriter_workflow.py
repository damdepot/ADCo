"""Offline tests for the ADCo rewriter workflow (no LLM, no network)."""

import asyncio

from src.code_rewriter.workflow import create_rewriter_workflow, make_orchestrate


def test_create_rewriter_workflow_builds():
    wf = create_rewriter_workflow(model="fake-model")
    assert wf.name == "adco_rewriter"


class _FakeCtx:
    def __init__(self, state, run_node):
        self.state = state
        self._run_node = run_node

    async def run_node(self, node, node_input=None):
        return await self._run_node(node, node_input)


async def _collect(agen):
    events = []
    async for event in agen:
        events.append(event)
    return events


def test_orchestrate_retries_then_passes():
    fake_optimizer = object()
    fake_verify = object()
    state = {
        "rewrite_contracts": [{"target": {"file": "a.py", "function": "f"}}],
        "current_contract": None,
    }
    verify_calls = {"n": 0}
    total_calls = {"n": 0}

    async def run_node(node, node_input=None):
        total_calls["n"] += 1
        if node is fake_verify:
            verify_calls["n"] += 1
            if verify_calls["n"] == 1:
                return {"status": "FAIL", "summary": "residual loop"}
            return {"status": "PASS"}
        return None

    ctx = _FakeCtx(state, run_node)
    orchestrate = make_orchestrate(fake_optimizer, fake_verify, max_attempts=3)
    events = asyncio.run(_collect(orchestrate(ctx)))

    final = events[-1]
    assert final.output["results"][0]["status"] == "PASS"
    # 2 optimizer + 2 verify calls
    assert total_calls["n"] == 4


def test_orchestrate_records_failure_when_exhausted():
    fake_optimizer = object()
    fake_verify = object()
    state = {
        "rewrite_contracts": [{"target": {"file": "a.py", "function": "f"}}],
        "current_contract": None,
    }

    async def run_node(node, node_input=None):
        if node is fake_verify:
            return {"status": "FAIL", "summary": "still looping"}
        return None

    ctx = _FakeCtx(state, run_node)
    orchestrate = make_orchestrate(fake_optimizer, fake_verify, max_attempts=3)
    events = asyncio.run(_collect(orchestrate(ctx)))

    final = events[-1]
    assert final.output["results"][0]["status"] == "FAIL"
    assert final.output["results"][0]["verification"]["summary"] == "still looping"
