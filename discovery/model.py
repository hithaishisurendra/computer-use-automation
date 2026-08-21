"""Provider-neutral model interface for the discovery loop.

The loop needs one thing from a model: given a system prompt, a transcript,
and a tool vocabulary, return the tool calls to make next. That is the whole
interface, and everything provider-specific lives behind it.

The seam is worth stating precisely, because it is easy to build a fake one.
The neutral pieces are:

- `ToolSpec` -- a name, a description, and a plain JSON Schema. Not an
  Anthropic `input_schema`, not a Gemini `FunctionDeclaration`.
- The transcript -- `Observation`, `AssistantAction`, `ActionResult`. These
  describe what happened, not how any provider encodes it.
- `ModelResponse` -- reasoning text plus `ToolCall`s.

Each client translates that neutral form into its own wire format and back.
Nothing downstream of `complete()` knows which provider ran: the recorder,
the artifact schema and replay never import a provider client, and a test
asserts it.

Keeping both implementations is the point. A single-implementation
"interface" is an untested guess about where the boundary is; two real
implementations are what demonstrate the boundary is in the right place.
"""

from __future__ import annotations

import os
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Union

DEFAULT_MODELS = {
    # gemini-2.5-flash is retired for new API keys -- it still appears in
    # models.list() but returns 404 NOT_FOUND ("no longer available to new
    # users") on generateContent. 3.5-flash is the current free-tier flash
    # model and was verified against this key before being made the default.
    "gemini": "gemini-3.5-flash",
    "anthropic": "claude-sonnet-5",
}

# Free-tier Gemini rate-limits aggressively, so retries are the normal case
# rather than an edge case. Bounded so a run cannot hang indefinitely.
MAX_RETRIES = 6
BASE_BACKOFF_S = 2.0
MAX_BACKOFF_S = 60.0


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env loader. Existing environment variables win, so an
    explicitly exported key is never silently overridden by a stale file."""
    file = Path(path)
    if not file.exists():
        return
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


# ---------------------------------------------------------------------------
# Neutral vocabulary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    """One callable action, described in plain JSON Schema."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class Observation:
    """What the agent sees before deciding."""

    text: str


@dataclass
class AssistantAction:
    """A tool call the model made."""

    call_id: str
    name: str
    arguments: dict[str, Any]
    text: str = ""


@dataclass
class ActionResult:
    """What executing that call produced."""

    call_id: str
    name: str
    text: str
    is_error: bool = False


Message = Union[Observation, AssistantAction, ActionResult]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ModelResponse:
    text: str = ""
    calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)


class RateLimited(Exception):
    """Provider refused for rate-limit reasons; retryable."""


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------


class ModelClient(ABC):
    """One method. Provider choice is configuration, not code."""

    provider: str = "unknown"

    def __init__(self, model: str, on_retry: Optional[Callable[[dict], None]] = None):
        self.model = model
        self._on_retry = on_retry

    @abstractmethod
    def complete(
        self, system: str, messages: list[Message], tools: list[ToolSpec]
    ) -> ModelResponse:
        """Return the model's next tool calls given the transcript so far."""

    # -- shared retry -------------------------------------------------------

    def _with_backoff(self, call: Callable[[], Any]) -> Any:
        """Exponential backoff with jitter on rate limits.

        Every retry is reported through `on_retry` so it lands in the run's
        evidence: a run that quietly took four minutes of backoff looks
        identical to a slow model unless the waiting is recorded.
        """
        attempt = 0
        while True:
            try:
                return call()
            except RateLimited as exc:
                attempt += 1
                if attempt > MAX_RETRIES:
                    raise
                delay = min(BASE_BACKOFF_S * (2 ** (attempt - 1)), MAX_BACKOFF_S)
                delay += random.uniform(0, delay * 0.25)  # jitter, avoid lockstep
                if self._on_retry:
                    self._on_retry(
                        {
                            "provider": self.provider,
                            "model": self.model,
                            "attempt": attempt,
                            "max_retries": MAX_RETRIES,
                            "sleep_s": round(delay, 2),
                            "reason": str(exc)[:400],
                        }
                    )
                time.sleep(delay)


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


class AnthropicClient(ModelClient):
    """Alternate implementation. Kept because it is what proves the seam:
    the neutral transcript above has to survive two genuinely different wire
    formats, and only a second implementation can show that it does."""

    provider = "anthropic"

    def __init__(self, model: str = DEFAULT_MODELS["anthropic"], on_retry=None):
        super().__init__(model, on_retry)
        import anthropic

        self._sdk = anthropic
        self._client = anthropic.Anthropic()

    def _to_wire(self, messages: list[Message]) -> list[dict[str, Any]]:
        wire: list[dict[str, Any]] = []
        for message in messages:
            if isinstance(message, Observation):
                wire.append({"role": "user", "content": message.text})
            elif isinstance(message, AssistantAction):
                content: list[dict[str, Any]] = []
                if message.text:
                    content.append({"type": "text", "text": message.text})
                content.append(
                    {
                        "type": "tool_use",
                        "id": message.call_id,
                        "name": message.name,
                        "input": message.arguments,
                    }
                )
                wire.append({"role": "assistant", "content": content})
            elif isinstance(message, ActionResult):
                wire.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": message.call_id,
                                "content": message.text,
                                "is_error": message.is_error,
                            }
                        ],
                    }
                )
        return wire

    def complete(self, system, messages, tools) -> ModelResponse:
        wire_tools = [
            {"name": t.name, "description": t.description, "input_schema": t.parameters}
            for t in tools
        ]

        def call():
            try:
                return self._client.messages.create(
                    model=self.model,
                    max_tokens=16000,
                    thinking={"type": "adaptive"},
                    system=[
                        {
                            "type": "text",
                            "text": system,
                            # Stable across every turn of the run.
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    tools=wire_tools,
                    messages=self._to_wire(messages),
                )
            except self._sdk.RateLimitError as exc:
                raise RateLimited(str(exc)) from exc

        response = self._with_backoff(call)
        return ModelResponse(
            text=" ".join(b.text for b in response.content if b.type == "text").strip(),
            calls=[
                ToolCall(id=b.id, name=b.name, arguments=dict(b.input))
                for b in response.content
                if b.type == "tool_use"
            ],
            usage={
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
        )


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------


class GeminiClient(ModelClient):
    provider = "gemini"

    def __init__(self, model: str = DEFAULT_MODELS["gemini"], on_retry=None):
        super().__init__(model, on_retry)
        from google import genai
        from google.genai import errors, types

        self._types = types
        self._errors = errors
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is not set. Put it in .env (gitignored) or export it."
            )
        self._client = genai.Client(api_key=api_key)
        self._call_seq = 0

    def _next_call_id(self) -> str:
        self._call_seq += 1
        return f"gem_{self._call_seq}"

    def _to_wire(self, messages: list[Message]) -> list[Any]:
        types = self._types
        contents: list[Any] = []
        for message in messages:
            if isinstance(message, Observation):
                contents.append(
                    types.Content(role="user", parts=[types.Part(text=message.text)])
                )
            elif isinstance(message, AssistantAction):
                parts = []
                if message.text:
                    parts.append(types.Part(text=message.text))
                parts.append(
                    types.Part(
                        function_call=types.FunctionCall(
                            name=message.name, args=message.arguments
                        )
                    )
                )
                contents.append(types.Content(role="model", parts=parts))
            elif isinstance(message, ActionResult):
                contents.append(
                    types.Content(
                        role="user",
                        parts=[
                            types.Part(
                                function_response=types.FunctionResponse(
                                    name=message.name,
                                    # Gemini requires a structured response
                                    # payload; the neutral form carries text,
                                    # so it is wrapped rather than reshaped.
                                    response={
                                        "result": message.text,
                                        "is_error": message.is_error,
                                    },
                                )
                            )
                        ],
                    )
                )
        return contents

    def complete(self, system, messages, tools) -> ModelResponse:
        types = self._types
        declarations = [
            types.FunctionDeclaration(
                name=t.name,
                description=t.description,
                # Plain JSON Schema straight through -- no per-provider
                # schema translation, so the vocabulary cannot drift in
                # meaning between providers.
                parameters_json_schema=t.parameters,
            )
            for t in tools
        ]
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[types.Tool(function_declarations=declarations)],
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    # ANY forces a call from the declared set every turn: the
                    # vocabulary stays closed, and "no tool call" -- which the
                    # loop would otherwise have to treat as a failed cycle --
                    # cannot happen.
                    mode=types.FunctionCallingConfigMode.ANY
                )
            ),
            temperature=0,
        )

        def call():
            try:
                return self._client.models.generate_content(
                    model=self.model,
                    contents=self._to_wire(messages),
                    config=config,
                )
            except self._errors.ClientError as exc:
                if getattr(exc, "code", None) == 429 or "RESOURCE_EXHAUSTED" in str(exc):
                    raise RateLimited(str(exc)) from exc
                raise
            except self._errors.ServerError as exc:
                # 5xx from the free tier behaves like a rate limit in
                # practice; backing off is the right response either way.
                raise RateLimited(str(exc)) from exc

        response = self._with_backoff(call)

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for candidate in response.candidates or []:
            for part in (candidate.content.parts if candidate.content else []) or []:
                if getattr(part, "text", None) and not getattr(part, "thought", False):
                    text_parts.append(part.text)
                function_call = getattr(part, "function_call", None)
                if function_call is not None:
                    calls.append(
                        ToolCall(
                            id=function_call.id or self._next_call_id(),
                            name=function_call.name,
                            arguments=dict(function_call.args or {}),
                        )
                    )

        usage = {}
        if response.usage_metadata:
            usage = {
                "input_tokens": response.usage_metadata.prompt_token_count,
                "output_tokens": response.usage_metadata.candidates_token_count,
            }
        return ModelResponse(text=" ".join(text_parts).strip(), calls=calls, usage=usage)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

PROVIDERS: dict[str, type[ModelClient]] = {
    "anthropic": AnthropicClient,
    "gemini": GeminiClient,
}


def build_client(
    provider: str, model: Optional[str] = None, on_retry=None
) -> ModelClient:
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r}; expected one of {sorted(PROVIDERS)}")
    return PROVIDERS[provider](model or DEFAULT_MODELS[provider], on_retry=on_retry)
