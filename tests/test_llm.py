"""Structured answers: the fallbacks that keep a provider's quirks from stopping the workflow."""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_groq import ChatGroq as RealChatGroq

from agents.analysis_agent import CompetitorList, ContentStrategy, ResearchQueries, SourcePicks
from agents.generation_agent import OutlineModel, RevisionDecision
from config import load_settings
from llm.provider import get_chat_model
from llm.structured import ask_structured, methods_for

MESSAGES = [SystemMessage("You are a test."), HumanMessage("Plan the searches.")]


class BadRequestError(Exception):
    """Same class name as the Groq SDK's."""


class AuthenticationError(Exception):
    pass


class ChatGroq:
    """Same class name as langchain's, so ask_structured picks the Groq order. Scripted outcomes."""

    def __init__(self, model="openai/gpt-oss-120b", fail=None, text=""):
        self.model_name = model
        self.fail = fail or {}             # method -> exception to raise
        self.text = text
        self.tried = []

    def with_structured_output(self, schema, method=None):
        self.tried.append(method)
        error = self.fail.get(method)

        class Runnable:
            def invoke(_, messages):
                if error:
                    raise error
                return schema(queries=[f"via {method}"])
        return Runnable()

    def invoke(self, messages):
        self.tried.append("text")
        assert "matches this JSON schema" in messages[-1].content
        return AIMessage(content=self.text)


TOOL_FAILED = BadRequestError("Error code: 400 - {'code': 'tool_use_failed', 'message': "
                              "'Tool choice is required, but model did not call a tool'}")


def test_gpt_oss_uses_structured_outputs_first():
    llm = ChatGroq()
    assert ask_structured(llm, ResearchQueries, MESSAGES).queries == ["via json_schema"]
    assert llm.tried == ["json_schema"]
    assert methods_for(ChatGroq(model="qwen/qwen3.8-27b")) == ["function_calling", "json_schema"]


def test_a_skipped_tool_call_falls_back_to_the_next_method():
    llm = ChatGroq(fail={"json_schema": TOOL_FAILED})
    assert ask_structured(llm, ResearchQueries, MESSAGES).queries == ["via function_calling"]
    assert llm.tried == ["json_schema", "function_calling"]


def test_last_resort_reads_json_from_plain_text():
    llm = ChatGroq(fail={"json_schema": TOOL_FAILED, "function_calling": TOOL_FAILED},
                   text='Here you go:\n```json\n{"queries": ["agentic ai guide", "agentic ai examples"]}\n```')
    assert ask_structured(llm, ResearchQueries, MESSAGES).queries == ["agentic ai guide", "agentic ai examples"]
    assert llm.tried == ["json_schema", "function_calling", "text"]


def test_when_nothing_works_the_first_format_error_is_reported():
    llm = ChatGroq(fail={"json_schema": TOOL_FAILED, "function_calling": TOOL_FAILED}, text="no json here")
    with pytest.raises(BadRequestError) as failed:
        ask_structured(llm, ResearchQueries, MESSAGES)
    assert failed.value is TOOL_FAILED


@pytest.mark.parametrize("error", [AuthenticationError("bad key"),
                                   BadRequestError("Error code: 400 - context length exceeded")])
def test_other_errors_are_not_retried_another_way(error):
    llm = ChatGroq(fail={"json_schema": error})
    with pytest.raises(type(error)):
        ask_structured(llm, ResearchQueries, MESSAGES)
    assert llm.tried == ["json_schema"]


@pytest.mark.parametrize("schema", [ResearchQueries, CompetitorList, SourcePicks, ContentStrategy,
                                    OutlineModel, RevisionDecision])
def test_every_agent_schema_converts_for_groq_structured_outputs(schema):
    llm = RealChatGroq(model="openai/gpt-oss-120b", api_key="gsk_test")
    bound = llm.with_structured_output(schema, method="json_schema").first
    response_format = bound.kwargs["response_format"]
    assert response_format["type"] == "json_schema" and response_format["json_schema"]["schema"]["properties"]


def test_gpt_oss_reasons_briefly_by_default():
    settings = load_settings(llm_provider="groq", llm_model="openai/gpt-oss-120b", groq_api_key="gsk_test")
    assert get_chat_model(settings).reasoning_effort == "low"
    chosen = load_settings(llm_provider="groq", llm_model="openai/gpt-oss-120b", groq_api_key="gsk_test",
                           llm_reasoning_effort="medium")
    assert get_chat_model(chosen).reasoning_effort == "medium"
