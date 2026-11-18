"""A fake LangChain chat model for the unit tests: no network, no cost.

It plays back scripted replies (text pieces and/or tool calls) and remembers every
request, so tests can check what the agents sent. It is a real ``BaseChatModel``,
so the LangGraph graphs stream and call tools exactly as they do with OpenAI.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Callable

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import Field


def say(*pieces: str) -> dict[str, Any]:
    """A scripted reply made of text pieces."""
    return {"text": list(pieces), "calls": []}


def call(name: str, call_id: str | None = None, text: str = "", **arguments: Any) -> dict[str, Any]:
    """A scripted reply that asks for one tool (optionally after some text)."""
    return calls((name, call_id, arguments), text=text)


def calls(*specs: tuple[str, str | None, dict[str, Any]], text: str = "") -> dict[str, Any]:
    tool_calls = [
        {"name": name, "args": arguments, "id": call_id or f"call_{name}_{uuid.uuid4().hex[:6]}"}
        for name, call_id, arguments in specs
    ]
    return {"text": [text] if text else [], "calls": tool_calls}


Reply = dict[str, Any] | Callable[[list[BaseMessage]], dict[str, Any]]


class FakeChatModel(BaseChatModel):
    replies: list[Any] = Field(default_factory=list)
    requests: list[dict[str, Any]] = Field(default_factory=list)
    model_name: str = "fake-model"
    default: Any = None  # used once the scripted replies run out (None: running out is an error)

    @property
    def _llm_type(self) -> str:
        return "fake-scripted"

    def add(self, *replies: Reply) -> None:
        self.replies.extend(replies)

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=tools, **kwargs)

    def _next(self, messages: list[BaseMessage], kwargs: dict[str, Any]) -> dict[str, Any]:
        self.requests.append({"messages": list(messages), **kwargs})
        if not self.replies and self.default is None:
            raise AssertionError("FakeChatModel ran out of scripted replies")
        reply = self.replies.pop(0) if self.replies else self.default
        return reply(messages) if callable(reply) else reply

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        reply = self._next(messages, kwargs)
        message = AIMessage(
            content="".join(reply["text"]),
            tool_calls=[{**c, "type": "tool_call"} for c in reply["calls"]],
            usage_metadata={"input_tokens": 5, "output_tokens": 5, "total_tokens": 10},
        )
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        reply = self._next(messages, kwargs)
        for piece in reply["text"]:
            chunk = ChatGenerationChunk(message=AIMessageChunk(content=piece))
            if run_manager:
                run_manager.on_llm_new_token(piece, chunk=chunk)
            yield chunk
        if reply["calls"]:
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {"name": c["name"], "args": json.dumps(c["args"]), "id": c["id"], "index": i}
                        for i, c in enumerate(reply["calls"])
                    ],
                )
            )
        yield ChatGenerationChunk(
            message=AIMessageChunk(content="", usage_metadata={"input_tokens": 5, "output_tokens": 5, "total_tokens": 10})
        )
