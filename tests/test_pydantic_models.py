from pydantic import BaseModel

from src.code_checker.agent import create_checker_agent
from src.code_rewriter.sub_agents.verifier.agent import create_verifier_agent
from src.code_rewriter.sub_agents.optimizer.agent import create_optimizer_agent
from src.intent_analyzer.sub_agents.file_selector.agent import create_file_selector_agent as create_intent_fs_agent
from src.intent_analyzer.sub_agents.intent_extractor.agent import create_intent_extractor_agent as create_intent_ie_agent
from src.knob_tuner.sub_agents.db_inspector.agent import create_db_inspector_agent
from src.knob_tuner.sub_agents.knob_recommender.agent import create_knob_recommender_agent

from src.intent_analyzer.models import IntentAnalyzerResult
from src.adco.models import AdcoPipelineResult


def test_subagent_output_schemas():
    agents = [
        create_checker_agent(),
        create_verifier_agent(),
        create_optimizer_agent(),
        create_intent_fs_agent(),
        create_intent_ie_agent(),
        create_db_inspector_agent(),
        create_knob_recommender_agent(),
    ]
    for agent in agents:
        assert getattr(agent, "output_schema", None) is not None, f"Agent {agent.name} missing output_schema"
        assert issubclass(agent.output_schema, BaseModel), f"Agent {agent.name} output_schema is not a BaseModel"


def test_intent_analyzer_result_model():
    data = {
        "timestamp": "2026-09-16T12:00:00",
        "target": "/path/to/app",
        "model": "gemini-3.5-flash-lite",
        "intent_output": {"queries": "SELECT 1"},
    }
    model = IntentAnalyzerResult(**data)
    assert model.target == "/path/to/app"
    assert model.intent_output["queries"] == "SELECT 1"
    assert model.model_dump() == data


def test_adco_pipeline_result_model():
    data = {
        "timestamp": "2026-09-16T12:00:00",
        "mode": "all",
        "target": "/path/to/app",
        "sandbox": "/path/to/sandbox",
        "model": "gemini-3.5-flash-lite",
        "intent_analyzer": {"status": "PASS"},
        "rewriter": {"status": "PASS"},
        "knob_tuner": {"status": "PASS"},
    }
    model = AdcoPipelineResult(**data)
    assert model.mode == "all"
    assert model.model_dump() == data
