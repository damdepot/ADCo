from pydantic import BaseModel

from src.code_checker.agent import create_checker_agent
from src.code_rewriter.sub_agents.verifier.agent import create_verifier_agent
from src.code_rewriter.sub_agents.optimizer.agent import create_optimizer_agent
from src.intent_analyzer.sub_agents.file_selector.agent import create_file_selector_agent as create_intent_fs_agent
from src.intent_analyzer.sub_agents.intent_extractor.agent import create_intent_extractor_agent as create_intent_ie_agent
from src.knob_tuner.sub_agents.db_inspector.agent import create_db_inspector_agent
from src.knob_tuner.sub_agents.intent_analyzer.agent import create_intent_analyzer_agent as create_tuner_ia_agent
from src.knob_tuner.sub_agents.knob_recommender.agent import create_knob_recommender_agent
from src.knob_tuner.sub_agents.knob_checker.agent import create_knob_checker_agent
from src.knob_tuner.sub_agents.live_tuner.agent import create_live_tuner_agent

from src.intent_analyzer.models import IntentAnalyzerResult
from src.knob_tuner.models import KnobTunerResult
from src.adco.models import AdcoPipelineResult


def test_subagent_output_schemas():
    agents = [
        create_checker_agent(),
        create_verifier_agent(),
        create_optimizer_agent(),
        create_intent_fs_agent(),
        create_intent_ie_agent(),
        create_db_inspector_agent(),
        create_tuner_ia_agent(),
        create_knob_recommender_agent(),
        create_knob_checker_agent(),
        create_live_tuner_agent(),
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


def test_knob_tuner_result_model():
    data = {
        "timestamp": "2026-09-16T12:00:00",
        "target": "/path/to/app",
        "db_type": "postgres",
        "db_name": "testdb",
        "cpu_cores": 2,
        "memory_gb": 4.0,
        "production_db": False,
        "dry_run": False,
        "staging_validated": True,
        "status": "PASS",
        "validation_attempt_count": 1,
        "intent_analyzer_output": {},
        "knob_recommender_output": {"max_connections": 100},
        "knob_checker_output": {"status": "PASS"},
        "live_tuner_output": {},
        "outputs": {
            "db_inspector": {},
            "intent_analyzer": {},
            "knob_recommender": {},
            "knob_checker": {},
            "live_tuner": {},
        },
    }
    model = KnobTunerResult(**data)
    assert model.status == "PASS"
    assert model.staging_validated is True


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
