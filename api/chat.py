"""A conversational front door standing in for the AI agent.

Thin by construction. It does exactly three things: turn a request into a
capability plus typed arguments, call the API, and say what happened in plain
language. It holds no state, drives no browser, and knows nothing about the
target application beyond what `GET /capabilities` returns.

The model's job is deliberately narrow. It picks a capability and fills its
declared arguments -- that is all. It does not decide whether a step is risky,
whether a capability may run, what the policy is, or how a result should be
classified. Every one of those is settled before the model is asked anything,
and none of its output can change them: the only thing this module does with a
tool call is forward it to an endpoint that would have refused an unauthorised
request from any other caller.

The reporting is the point. A result classification is a contract the rest of
the system has been careful about, and the easiest way to throw that away is a
chat layer that renders everything non-success as "sorry, something went
wrong". A business outcome is an ANSWER -- "no such member", "that share is on
hold", "you are not authorised" -- and phrasing it as a failure would undo the
distinction the brief calls the most common design mistake.
"""

from __future__ import annotations

from typing import Any, Optional

from discovery.model import (
    Message,
    ModelClient,
    Observation,
    ToolSpec,
    build_client,
    load_dotenv,
)

# What the model is told, and what it is not. The prohibitions are worth
# stating explicitly even though they are structurally impossible: a model
# that believes it can escalate its own privileges wastes turns trying.
SYSTEM_PROMPT = """\
You turn a bank operator's request into one call to a recorded capability.

Each tool below is a capability that has already been recorded against a live \
back-office application. Its parameters are the capability's own declared \
inputs. Pick the one that matches the request and fill in its arguments from \
what the user said.

RULES
1. Call exactly one tool, or none.
2. Only use argument values the user actually supplied, or that the capability \
   declares a default for. Never invent an account number, a member number, a \
   share id or an amount. If the request is missing something the capability \
   requires, call no tool and say what is missing.
3. If no capability matches, or you are not confident which one does, call no \
   tool. Say so and let the caller choose. This matters most for capabilities \
   that move money: guessing at one is worse than asking.
4. Some capabilities need a supervisor and some contain an irreversible step. \
   That is handled for you -- do not try to work around it, and do not \
   promise the user that an irreversible action will complete.

You are not driving a user interface. You are choosing a capability and its \
arguments; something else runs it."""

# Types a capability may declare, mapped to JSON Schema. The vocabulary is
# the artifact's, not this module's -- a capability that declares a type
# nothing here understands should surface as a schema error, not be silently
# coerced to a string.
_JSON_TYPES = {
    "string": "string",
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
}


def capability_tools(catalog: list[dict[str, Any]]) -> list[ToolSpec]:
    """The catalogue, as the model's callable vocabulary.

    Built from the same `GET /capabilities` body a calling agent reads, so
    the model cannot see a capability the API would not serve, or an argument
    the artifact does not declare. A capability that will not load, or whose
    flow never completed, is simply absent -- there is nothing to call.
    """
    tools: list[ToolSpec] = []
    for entry in catalog:
        if not entry.get("invocable"):
            continue
        properties: dict[str, Any] = {}
        required: list[str] = []
        for spec in entry.get("inputs", []):
            described = spec.get("description", "")
            if spec.get("example") is not None:
                described += f" Example: {spec['example']}."
            properties[spec["name"]] = {
                "type": _JSON_TYPES.get(spec.get("type", "string"), "string"),
                "description": described.strip(),
            }
            if spec.get("required"):
                required.append(spec["name"])

        description = entry.get("description") or entry.get("name") or entry["id"]
        # Facts that change whether a caller should pick this capability at
        # all. An agent that learns "this needs a supervisor" only from a 403
        # has learned by failing.
        if entry.get("required_role"):
            description += f" Requires a {entry['required_role']}."
        if entry.get("requires_human"):
            description += (
                " Contains an irreversible step: an unattended run stops there "
                "and asks a person to complete it."
            )
        tools.append(ToolSpec(
            name=entry["id"],
            description=description,
            parameters={"type": "object", "properties": properties, "required": required},
        ))
    return tools


class Chat:
    """One request in, one capability invocation out."""

    def __init__(self, client: Optional[ModelClient] = None, provider: str = "anthropic",
                 model: Optional[str] = None):
        # The discovery loop's provider interface, unchanged. A second model
        # client would be a second place for a provider quirk to live.
        self._client = client
        self._provider = provider
        self._model = model

    @property
    def client(self) -> ModelClient:
        if self._client is None:
            # The CLIs load .env themselves; a server process never did, so
            # the chat endpoint reported "could not reach the model" on a
            # machine where the key was sitting in .env the whole time. Built
            # lazily so an API with no key still serves every other endpoint.
            load_dotenv()
            self._client = build_client(self._provider, self._model)
        return self._client

    def choose(self, request: str, catalog: list[dict[str, Any]]) -> dict[str, Any]:
        """Map a request to a capability and arguments, or decline.

        Returns `{"capability": id, "inputs": {...}}` on a confident match,
        or `{"message": ...}` when the model declined. Declining is a correct
        outcome, not a failure: the alternative is guessing at a capability
        that might move money.
        """
        tools = capability_tools(catalog)
        if not tools:
            return {"message": "No capabilities are currently invocable."}

        messages: list[Message] = [Observation(text=request)]
        response = self.client.complete(SYSTEM_PROMPT, messages, tools)

        if not response.calls:
            return {
                "message": response.text.strip() or (
                    "I could not match that to a recorded capability."),
                "available": [t.name for t in tools],
            }
        call = response.calls[0]
        return {
            "capability": call.name,
            "inputs": dict(call.arguments),
            "reasoning": response.text.strip(),
            "usage": response.usage,
        }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _outputs_sentence(outputs: dict[str, Any]) -> str:
    if not outputs:
        return "It completed and returned no values."
    parts = [f"{name.replace('_', ' ')} is {value}" for name, value in outputs.items()]
    return "The " + ", and the ".join(parts) + "."


def describe(result: dict[str, Any], capability: str, inputs: dict[str, Any]) -> str:
    """Say what happened, in language an operator would use.

    One branch per classification, because the classifications mean genuinely
    different things and collapsing them is how a legitimate answer starts
    reading as a crash.
    """
    classification = result.get("classification") or result.get("status") or "unknown"

    if classification == "success":
        return f"Done. {_outputs_sentence(result.get('outputs') or {})}"

    if classification == "business_outcome":
        # An ANSWER. The application was asked a question and gave one, so
        # this is phrased as a result rather than a problem -- no "failed",
        # no "error", no apology.
        message = result.get("message") or "The application returned a business outcome."
        return message

    if classification == "escalation_required":
        escalation = result.get("escalation") or {}
        step = escalation.get("step_id") or "an irreversible step"
        expected = escalation.get("expected_on_resume")
        lines = [
            f"I stopped before completing this. Step {step} is irreversible, and this "
            "capability's policy requires a person to perform it rather than "
            "automation.",
            "Everything up to that point is done and the session is still open.",
        ]
        if expected:
            lines.append(f"When someone completes it, the run will verify: {expected}.")
        lines.append(
            "Open the Interventions tab to see the captured state and hand control "
            "back when the step has been performed.")
        return " ".join(lines)

    if classification == "caller_error":
        violations = result.get("violations") or []
        if violations:
            detail = "; ".join(
                f"{v.get('input')}: {v.get('message')}" for v in violations)
            return f"I could not run that: {detail}"
        return f"I could not run that: {result.get('message', 'the arguments were invalid')}"

    if classification == "auth_failure":
        # Explicitly not the user's problem. Telling an operator to check
        # their input when the system's own credentials are wrong sends them
        # somewhere unhelpful.
        return (
            "This did not run because of a system configuration problem, not anything "
            f"you did: {result.get('message', 'credentials could not be resolved')}. "
            "The system's own credentials for this application need attention.")

    failure = result.get("failure") or {}
    parts = [f"This failed at step {failure.get('step_id', 'an unknown step')}."]
    if failure.get("expected"):
        parts.append(f"Expected {failure['expected']}.")
    if failure.get("observed"):
        parts.append(f"Observed {failure['observed']}.")
    if result.get("message") and not failure:
        parts.append(result["message"])
    parts.append(f"The full trace is on the run detail page for {result.get('run_id', '')}.")
    return " ".join(p for p in parts if p).strip()
