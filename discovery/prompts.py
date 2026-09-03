"""System prompt and the closed tool vocabulary for the discovery loop.

The tool list *is* the action vocabulary. The model cannot invent an action,
because an action that is not a declared tool has no way to reach the
executor -- the constraint is structural, not a request in the prompt. The
seven tools mirror the seven artifact actions exactly, so anything the model
does during discovery is expressible in the recorded artifact by
construction; there is no "discovery could do it but replay cannot" gap.

Two extra tools bracket the run: `goal_reached` and `stuck`. Requiring an
explicit terminal call is what separates "the model finished" from "the
model stopped producing tool calls", which otherwise look identical.
"""

from __future__ import annotations

from typing import Any, Optional

from discovery.model import ToolSpec

SYSTEM_PROMPT = """\
You are driving a real web application to accomplish a goal, one action at a \
time. You are operating a live system on behalf of an operator.

GOAL
{goal}

TARGET
Base URL: {base_url}
Entry path: {entry_path}
You are already authenticated. The session is established; do not attempt to \
log in, and do not look for a login form.

HOW YOU SEE THE PAGE
After every action you receive an accessibility snapshot of each frame: one \
line per element, showing its role, its accessible name in quotes, and any \
state. This is the same representation a screen reader would use. You do not \
get a screenshot, and you do not get HTML or CSS. Target elements by their \
role and accessible name, never by position on screen.

{frames_section}
TARGETING ELEMENTS
Every action that touches an element takes {target_params}. \
`name` must match the accessible name in the snapshot exactly, including \
capitalisation.

When several elements share a role and name -- for example a "View" link in \
every row of a results table -- add `row_contains` with text that appears in \
the row you want. That is how you say "the View link in the row for member \
12345" instead of "the third View link".

To read a value out of a table row, use `extract` with `row_contains` to \
select the row and `column_header` to select the column.

ALLOWLIST
You may only act within these paths on the target host:
{allowed_paths}

Allowed actions: {allowed_actions}

These are enforced outside your control. An action outside them is refused \
and the run stops. Do not try to reach any other path, and do not try to \
change the application's configuration or internal state.

RULES
1. Take one action at a time and read the resulting snapshot before deciding \
   the next one. Do not guess what a page will look like.
2. Before acting on an element, confirm it is present in the snapshot you \
   were just given.
3. If an action does not change the page the way you expected, read the \
   snapshot again and adapt. Do not repeat the identical failing action.
4. When the goal is accomplished, call `goal_reached`. You must call it \
   explicitly -- the run does not end on its own.
5. If you cannot proceed -- the control you need does not exist, the data is \
   not there, or you would have to leave the allowlist -- call `stuck` with a \
   specific reason. Calling `stuck` is a correct outcome, not a failure. Do \
   not guess or fabricate a value to appear successful.
6. Extract every value the goal asks you to read. A run that reaches the \
   right screen but reads nothing has not met the goal.
"""

# What the prompt says about frames, and whether it says anything at all.
# A frameset app needs the model to name a frame on every action; a
# single-document app has no frames to name, and teaching it a frame model
# that does not exist produces actions targeting a frame the page never had.
FRAMES_SECTION = """\
The application uses frames. Every action must name the frame it applies to, \
and the snapshot labels each one. The `{content_frame}` frame holds the main \
working area; the others hold persistent navigation and page furniture. The \
same control name can appear in more than one frame, so the frame is what \
disambiguates them. Unless you have a specific reason, act on the \
`{content_frame}` frame.

"""

# Shared targeting parameters. Kept identical across every element-touching
# tool so the model learns one addressing scheme rather than several.
_TARGET_PROPERTIES: dict[str, Any] = {
    "frame": {
        "type": "string",
        "description": "Frame the element is in, as labelled in the snapshot.",
    },
    "role": {
        "type": "string",
        "description": "Accessibility role, e.g. textbox, button, link, cell, combobox.",
    },
    "name": {
        "type": "string",
        "description": "Exact accessible name as shown in the snapshot.",
    },
    "row_contains": {
        "type": "string",
        "description": (
            "Optional. Text identifying the row containing the element, when role "
            "and name alone are ambiguous (e.g. a 'View' link repeated per row)."
        ),
    },
}


def _target_tool(
    name: str, description: str, extra: dict | None = None, required=None, framed: bool = True
) -> ToolSpec:
    properties = {**_TARGET_PROPERTIES, **(extra or {})}
    if not framed:
        properties.pop("frame", None)
    fields = list(required or ["frame", "role", "name"])
    if not framed:
        fields = [f for f in fields if f != "frame"]
    return ToolSpec(
        name=name,
        description=description,
        parameters={"type": "object", "properties": properties, "required": fields},
    )


# Declared once, in plain JSON Schema, and handed to whichever provider is
# configured. The vocabulary cannot drift in meaning between providers
# because there is only one definition of it.
def build_tools(content_frame: Optional[str] = None) -> list[ToolSpec]:
    """The action vocabulary, with frame targeting only where frames exist.

    A tool schema that *requires* a frame on an app with none forces the
    model to invent one, and an invented frame name resolves to nothing --
    which surfaces as "element not found" and reads like a perception
    failure rather than a prompt that described the wrong world.
    """
    framed = content_frame is not None
    return [_reframe(tool, framed) for tool in _TOOLS_FRAMED]


def _reframe(tool: ToolSpec, framed: bool) -> ToolSpec:
    if framed:
        return tool
    properties = {k: v for k, v in tool.parameters["properties"].items() if k != "frame"}
    required = [f for f in tool.parameters.get("required", []) if f != "frame"]
    return ToolSpec(
        name=tool.name,
        description=tool.description,
        parameters={"type": "object", "properties": properties, "required": required},
    )


_TOOLS_FRAMED: list[ToolSpec] = [
    ToolSpec(
        name="navigate",
        description=(
            "Load a path into a frame. Use only for paths on the allowlist. "
            "Prefer clicking a link the page actually offers over navigating "
            "directly, since a click is what a human operator would do."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path on the target host, beginning with '/'.",
                },
                "frame": {
                    "type": "string",
                    "description": "Frame to load the path into, as labelled in the snapshot.",
                },
            },
            "required": ["path", "frame"],
        },
    ),
    _target_tool("click", "Click a link or button."),
    _target_tool(
        "fill",
        "Type a value into a text input, replacing whatever is there.",
        extra={"value": {"type": "string", "description": "The text to type."}},
        required=["frame", "role", "name", "value"],
    ),
    _target_tool(
        "select",
        "Choose an option in a dropdown (combobox).",
        extra={"value": {"type": "string", "description": "The option to select."}},
        required=["frame", "role", "name", "value"],
    ),
    _target_tool("check", "Tick a checkbox or select a radio button."),
    _target_tool(
        "extract",
        (
            "Read a value off the page and return it as a named output. Use "
            "row_contains plus column_header to read a cell from a table row."
        ),
        extra={
            "output_name": {
                "type": "string",
                "description": "snake_case name for this output, e.g. savings_balance.",
            },
            "output_type": {
                "type": "string",
                "enum": ["string", "money", "integer", "number", "date", "boolean"],
                "description": (
                    "Type of the value. Use 'money' for currency amounts so the "
                    "caller receives a number rather than a formatted string."
                ),
            },
            "column_header": {
                "type": "string",
                "description": (
                    "Optional. Column header naming the cell to read within the "
                    "row selected by row_contains."
                ),
            },
        },
        required=["frame", "output_name", "output_type"],
    ),
    _target_tool(
        "wait_for",
        "Wait for an element to appear before continuing.",
    ),
    ToolSpec(
        name="goal_reached",
        description=(
            "Call when the goal is fully accomplished and every value it asks "
            "for has been extracted."
        ),
        parameters={
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "One sentence on what was accomplished.",
                },
            },
            "required": ["summary"],
        },
    ),
    ToolSpec(
        name="stuck",
        description=(
            "Call when you cannot proceed. This is a correct outcome when the "
            "goal genuinely cannot be met; it is not a failure to be avoided."
        ),
        parameters={
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Specifically what is blocking progress and what you tried.",
                },
            },
            "required": ["reason"],
        },
    ),
]

# Module-level default, kept so the vocabulary can be inspected without a
# profile. The frameless shape is the default because a frameset is the
# exception.
TOOLS: list[ToolSpec] = build_tools(None)

TERMINAL_TOOLS = {"goal_reached", "stuck"}
ACTION_TOOLS = {t.name for t in _TOOLS_FRAMED} - TERMINAL_TOOLS


def build_system_prompt(
    goal: str,
    base_url: str,
    entry_path: str,
    allowed_paths: list[str],
    allowed_actions: list[str],
    content_frame: Optional[str] = None,
) -> str:
    framed = content_frame is not None
    return SYSTEM_PROMPT.format(
        goal=goal,
        base_url=base_url,
        entry_path=entry_path,
        allowed_paths="\n".join(f"  {p}" for p in allowed_paths),
        allowed_actions=", ".join(allowed_actions),
        frames_section=(
            FRAMES_SECTION.format(content_frame=content_frame) if framed else ""
        ),
        target_params=(
            "`frame`, `role`, and `name`" if framed else "`role` and `name`"
        ),
    )
