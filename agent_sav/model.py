"""The language model: one call takes the conversation and the tools, and returns an AIMessage."""

import json
import os
from collections.abc import Callable
from queue import Empty, Queue
from threading import Thread

import httpx2
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langsmith import traceable
from mistralai.client import Mistral
from mistralai.client.errors import SDKError

MODEL = "zai-glm-5-3-flash"

DEADLINE = 60  # seconds for a whole call

Complete = Callable[[str, list[AnyMessage], list[dict]], AIMessage]


class UnusableAnswer(Exception):
    """The model answered nothing usable: cut short, empty, or malformed."""


def is_transient(error: Exception) -> bool:
    """Whether calling the model again may work."""
    if isinstance(error, SDKError):
        return error.status_code in (408, 429) or error.status_code >= 500
    return isinstance(error, (UnusableAnswer, httpx2.TransportError, TimeoutError))


def within(seconds: float, function: Callable):
    """Runs a call with an overall deadline. The SDK's timeout only bounds each read, and a call can hang for minutes."""
    results = Queue()

    def run() -> None:
        try:
            results.put((function(), None))
        except BaseException as error:
            results.put((None, error))

    Thread(target=run, daemon=True).start()
    try:
        value, error = results.get(timeout=seconds)
    except Empty:
        raise TimeoutError(f"Pas de réponse du modèle en {seconds} s.") from None
    if error:
        raise error
    return value


def to_mistral(message: AnyMessage) -> dict:
    if isinstance(message, HumanMessage):
        return {"role": "user", "content": str(message.text)}
    if isinstance(message, ToolMessage):
        return {"role": "tool", "tool_call_id": message.tool_call_id, "name": message.name, "content": str(message.text)}
    return {"role": "assistant", "content": str(message.text), "tool_calls": [
        {"id": call["id"], "type": "function",
         "function": {"name": call["name"], "arguments": json.dumps(call["args"], ensure_ascii=False)}}
        for call in message.tool_calls
    ] or None}


def visible_text(content) -> str:
    """The answer without the model's reasoning."""
    if isinstance(content, str):
        return content.strip()
    return "".join(chunk.text for chunk in content or [] if chunk.type == "text").strip()


def trace_outputs(message: AIMessage | None) -> dict:
    if message is None:  # the call failed
        return {}
    return {"content": message.text, "tool_calls": message.tool_calls, "usage_metadata": message.usage_metadata}


@traceable(name="Mistral chat", run_type="llm", process_outputs=trace_outputs,
           metadata={"ls_provider": "mistralai"})
def chat(messages: list[dict], tools: list[dict], model: str) -> AIMessage:
    with Mistral(api_key=os.environ["MISTRAL_API_KEY"], timeout_ms=45000) as client:
        result = within(DEADLINE, lambda: client.chat.complete(
            model=model, messages=messages, max_tokens=2048, reasoning_effort="high",
            **({"tools": tools, "tool_choice": "auto", "parallel_tool_calls": True} if tools else {}),
        ))
    choice = result.choices[0]
    text = visible_text(choice.message.content)
    try:
        calls = [{"id": call.id, "name": call.function.name, "args": (
            json.loads(call.function.arguments or "{}") if isinstance(call.function.arguments, str) else call.function.arguments
        )} for call in choice.message.tool_calls or []]
    except json.JSONDecodeError as error:
        raise UnusableAnswer("Appel d’outil illisible.") from error
    if choice.finish_reason == "length" or not (text or calls):
        raise UnusableAnswer("Réponse vide ou tronquée.")
    return AIMessage(content=text, tool_calls=calls, usage_metadata={
        "input_tokens": result.usage.prompt_tokens,
        "output_tokens": result.usage.completion_tokens,
        "total_tokens": result.usage.total_tokens,
    })


def complete(system: str, messages: list[AnyMessage], tools: list[dict]) -> AIMessage:
    return chat([{"role": "system", "content": system}, *map(to_mistral, messages)], tools,
                os.getenv("SAV_MODEL") or MODEL)
