"""OpenAI chat models of the NABD agents, built from ``ModelSettings``.

GPT-5 models only accept tools together with a reasoning effort through
OpenAI's Responses API, so every agent uses it. The API key is passed to the
client explicitly; NABD never reads the legacy voice ``.env`` or OPENAI_API_KEY.
"""
from __future__ import annotations

from dataclasses import dataclass

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

from nabd.settings import ModelSettings


def _chat(settings: ModelSettings, model: str, effort: str | None) -> ChatOpenAI:
    kwargs = {"reasoning": {"effort": effort}} if effort else {}
    return ChatOpenAI(model=model, api_key=settings.openai_api_key, use_responses_api=True, stream_usage=True,
                      timeout=settings.model_timeout_seconds, max_retries=2, **kwargs)


@dataclass(frozen=True)
class AgentModels:
    supervisor: BaseChatModel
    orders: BaseChatModel
    complaints: BaseChatModel


def build_models(settings: ModelSettings) -> AgentModels:
    return AgentModels(
        supervisor=_chat(settings, settings.supervisor_model, settings.supervisor_reasoning_effort),
        orders=_chat(settings, settings.orders_model, settings.orders_reasoning_effort),
        complaints=_chat(settings, settings.complaints_model, settings.complaints_reasoning_effort),
    )
