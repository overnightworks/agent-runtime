"""The agent file: Claude Code's subagent format plus ``appRoles`` and ``maxDurationSeconds``."""

from __future__ import annotations

import traceback
from pathlib import Path
from types import EllipsisType

import pytest
from pydantic import ValidationError

from agent_runtime_client.agents import AgentDefinition, AgentFileError, TurnRequest, load_agent
from agent_runtime_client.tools import ToolDeclaration, ToolEffect
from agent_runtime_client.turn import Turn, TurnMessage, TurnRole

READ_NOTE = ToolDeclaration(
    name="read_note", description="Read a note", parameters={}, effect=ToolEffect.READ_ONLY
)
EDIT_NOTE = ToolDeclaration(name="edit_note", description="Edit a note", parameters={})
DECLARATIONS = [READ_NOTE, EDIT_NOTE]

PROMPT = "You help with notes."


def agent_text(
    *,
    name: str = "notes-assistant",
    description: str | None = "Helps with notes",
    tools: str | None = "[read_note, edit_note]",
    roles: str | None = "[admin, user]",
    model: str | None = "text/opus",
    max_duration_seconds: str | None = "300",
    max_tool_calls: str | None | EllipsisType = ...,
    extra: str = "",
    body: str = PROMPT,
) -> str:
    lines = [f"name: {name}"]
    if description is not None:
        lines.append(f"description: {description}")
    if tools is not None:
        lines.append(f"tools: {tools}")
    if roles is not None:
        lines.append(f"appRoles: {roles}")
    if model is not None:
        lines.append(f"model: {model}")
    if max_duration_seconds is not None:
        lines.append(f"maxDurationSeconds: {max_duration_seconds}")
    if max_tool_calls is ...:
        max_tool_calls = None if tools in (None, "[]") else "5"
    if max_tool_calls is not None:
        lines.append(f"maxToolCalls: {max_tool_calls}")
    if extra:
        lines.append(extra)
    return "---\n" + "\n".join(lines) + "\n---\n" + body + "\n"


def with_field_replaced(front_matter: str) -> str:
    base = (
        "name: notes-assistant\ndescription: d\nappRoles: [admin]\n"
        "model: text/opus\nmaxDurationSeconds: 300\n"
    )
    replaced_key = front_matter.lstrip().split(":")[0]
    kept = "".join(line + "\n" for line in base.splitlines() if line.split(":")[0] != replaced_key)
    return kept + front_matter


def write(tmp_path: Path, text: str | bytes, stem: str = "notes-assistant") -> Path:
    path = tmp_path / f"{stem}.md"
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


def load(tmp_path: Path, text: str | bytes, stem: str = "notes-assistant", declarations=None):
    path = write(tmp_path, text, stem)
    return load_agent(path, DECLARATIONS if declarations is None else declarations)


def front_matter_file(tmp_path: Path, front_matter: str, body: str = PROMPT) -> Path:
    return write(tmp_path, f"---\n{front_matter}\n---\n{body}\n")


def test_full_file_loads_with_tools_in_file_order(tmp_path: Path) -> None:
    agent = load(tmp_path, agent_text(tools="[edit_note, read_note]"))

    assert agent.name == "notes-assistant"
    assert agent.description == "Helps with notes"
    assert agent.roles == ["admin", "user"]
    assert agent.tools == (EDIT_NOTE, READ_NOTE)
    assert agent.tools[1].effect is ToolEffect.READ_ONLY
    assert agent.prompt == PROMPT


def test_minimal_file_has_no_tools(tmp_path: Path) -> None:
    agent = load(tmp_path, agent_text(tools=None, roles="[admin]"))

    assert agent.tools == ()


@pytest.mark.parametrize(
    ("body", "prompt"),
    [
        ("\n\nFirst.\n\nSecond.\n\n", "First.\n\nSecond."),
        ("\n    code\n    next\n", "    code\n    next"),
        ("Line with hard break  \nnext  \n", "Line with hard break  \nnext  "),
        ("\n  \n  indented\n \n", "  indented"),
    ],
    ids=["blank-lines", "indented-code", "trailing-spaces", "blank-whitespace-lines"],
)
def test_prompt_drops_only_surrounding_blank_lines(tmp_path: Path, body: str, prompt: str) -> None:
    assert load(tmp_path, agent_text(body=body)).prompt == prompt


@pytest.mark.parametrize(
    "roles", ["[user]", "[]", "[user, admin]", "[admin, admin]", "admin", "[admin, no]", None]
)
def test_app_roles_other_than_the_two_lists_are_refused(tmp_path: Path, roles: str | None) -> None:
    path = write(tmp_path, agent_text(roles=roles))

    with pytest.raises(AgentFileError, match="appRoles"):
        load_agent(path, DECLARATIONS)


@pytest.mark.parametrize("key", ["color: red", "permissionMode: plan"])
def test_unknown_key_is_refused_and_named(tmp_path: Path, key: str) -> None:
    path = write(tmp_path, agent_text(extra=key))

    with pytest.raises(AgentFileError, match=key.split(":")[0]):
        load_agent(path, DECLARATIONS)


@pytest.mark.parametrize("name", ["other-name", "123", "'123'", '"notes-assistant2"'])
def test_name_differing_from_the_stem_is_refused(tmp_path: Path, name: str) -> None:
    path = write(tmp_path, agent_text(name=name))

    with pytest.raises(AgentFileError, match="name"):
        load_agent(path, DECLARATIONS)


def test_missing_name_is_refused(tmp_path: Path) -> None:
    path = write(tmp_path, agent_text().replace("name: notes-assistant\n", "", 1))

    with pytest.raises(AgentFileError, match="name"):
        load_agent(path, DECLARATIONS)


def test_undeclared_tool_is_refused_and_named(tmp_path: Path) -> None:
    path = write(tmp_path, agent_text(tools="[delete_note]"))

    with pytest.raises(AgentFileError, match="delete_note"):
        load_agent(path, DECLARATIONS)


def test_duplicate_tool_in_the_file_is_refused(tmp_path: Path) -> None:
    path = write(tmp_path, agent_text(tools="[read_note, read_note]"))

    with pytest.raises(AgentFileError, match="read_note"):
        load_agent(path, DECLARATIONS)


def test_two_declarations_with_one_name_are_refused(tmp_path: Path) -> None:
    twin = READ_NOTE.model_copy(update={"effect": ToolEffect.SIDE_EFFECTING})
    path = write(tmp_path, agent_text())

    with pytest.raises(AgentFileError, match="read_note"):
        load_agent(path, [READ_NOTE, twin])


@pytest.mark.parametrize("body", ["", "   \n\n  "])
def test_empty_prompt_is_refused(tmp_path: Path, body: str) -> None:
    path = write(tmp_path, agent_text(body=body))

    with pytest.raises(AgentFileError, match="prompt"):
        load_agent(path, DECLARATIONS)


@pytest.mark.parametrize(
    "stem", ["Notes", "notes.assistant", "notes_assistant", "a" + "b" * 64, "1notes", "-notes"]
)
def test_stem_that_is_not_an_agent_is_refused(tmp_path: Path, stem: str) -> None:
    path = write(tmp_path, agent_text(name=stem), stem=stem)

    with pytest.raises(AgentFileError, match=stem):
        load_agent(path, DECLARATIONS)


def test_file_without_md_suffix_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "notes-assistant.txt"
    path.write_text(agent_text(), encoding="utf-8")

    with pytest.raises(AgentFileError, match="notes-assistant.txt"):
        load_agent(path, DECLARATIONS)


@pytest.mark.parametrize(
    "text",
    [
        "name: notes-assistant\nappRoles: [admin]\n---\nbody\n",
        "---\nname: notes-assistant\nappRoles: [admin]\nbody\n",
        "---\nname: [unclosed\n---\nbody\n",
        "---\n- a\n- b\n---\nbody\n",
        "---\n---\nbody\n",
    ],
    ids=["no-opening", "no-closing", "broken-list", "not-a-mapping", "empty-front-matter"],
)
def test_malformed_front_matter_is_refused(tmp_path: Path, text: str) -> None:
    path = write(tmp_path, text)

    with pytest.raises(AgentFileError, match="notes-assistant.md"):
        load_agent(path, DECLARATIONS)


def test_bytes_that_are_not_utf8_are_refused(tmp_path: Path) -> None:
    path = write(tmp_path, b"---\nname: \xff\xfe\n---\nbody\n")

    with pytest.raises(AgentFileError, match="UTF-8"):
        load_agent(path, DECLARATIONS)


def test_the_definition_carries_the_files_model_and_duration(tmp_path: Path) -> None:
    agent = load(tmp_path, agent_text(model="text/opus", max_duration_seconds="300"))

    assert (agent.model, agent.max_duration_seconds) == ("text/opus", 300)


@pytest.mark.parametrize("field", ["model", "maxDurationSeconds"])
def test_a_file_without_a_required_key_is_refused_and_names_it(tmp_path: Path, field: str) -> None:
    text = agent_text(
        model=None if field == "model" else "text/opus",
        max_duration_seconds=None if field == "maxDurationSeconds" else "300",
    )

    with pytest.raises(AgentFileError, match=field):
        load(tmp_path, text)


@pytest.mark.parametrize(
    "model",
    [
        "image/x",
        "anthropic/opus",
        "claude/sonnet",
        "text/",
        "TEXT/opus",
        "text/A",
        "5",
        "null",
        "''",
    ],
)
def test_a_model_that_is_not_a_text_gateway_name_is_refused_and_named(
    tmp_path: Path, model: str
) -> None:
    text = agent_text(model=model)

    with pytest.raises(AgentFileError, match="model"):
        load(tmp_path, text)


@pytest.mark.parametrize("seconds", ["0", "-1", "true", "'300'", "1.5", "null", "''"])
def test_a_duration_that_is_not_a_positive_integer_is_refused_and_named(
    tmp_path: Path, seconds: str
) -> None:
    text = agent_text(max_duration_seconds=seconds)

    with pytest.raises(AgentFileError, match="maxDurationSeconds"):
        load(tmp_path, text)


def test_the_tool_call_bound_is_carried_by_the_definition_and_its_request(tmp_path: Path) -> None:
    agent = load(tmp_path, agent_text(max_tool_calls="7"))

    request = agent.request([TurnMessage(role=TurnRole.USER, text="hi")])

    assert (agent.max_tool_calls, request.max_tool_calls) == (7, 7)


@pytest.mark.parametrize("tools", [None, "[]"])
def test_an_agent_without_tools_has_no_tool_call_bound(tmp_path: Path, tools: str | None) -> None:
    agent = load(tmp_path, agent_text(tools=tools))

    assert agent.max_tool_calls is None


def test_a_missing_tool_call_bound_is_refused_when_there_are_tools(tmp_path: Path) -> None:
    text = agent_text(max_tool_calls=None)

    with pytest.raises(AgentFileError, match="maxToolCalls"):
        load(tmp_path, text)


@pytest.mark.parametrize("tools", [None, "[]"])
def test_a_tool_call_bound_is_refused_when_there_are_no_tools(
    tmp_path: Path, tools: str | None
) -> None:
    text = agent_text(tools=tools, max_tool_calls="3")

    with pytest.raises(AgentFileError, match="maxToolCalls"):
        load(tmp_path, text)


@pytest.mark.parametrize("bound", ["0", "-1", "true", "'3'", "1.5", "null", "''"])
def test_a_tool_call_bound_that_is_not_a_positive_integer_is_refused_and_named(
    tmp_path: Path, bound: str
) -> None:
    text = agent_text(max_tool_calls=bound)

    with pytest.raises(AgentFileError, match="maxToolCalls"):
        load(tmp_path, text)


def test_a_request_with_tools_and_no_bound_is_refused(tmp_path: Path) -> None:
    request = load(tmp_path, agent_text()).request([TurnMessage(role=TurnRole.USER, text="hi")])
    fields = {**dict(request), "max_tool_calls": None}

    with pytest.raises(ValidationError):
        TurnRequest(**fields)


@pytest.mark.parametrize("description", [None, "''", '"two\\nlines"', "|", "|\n  two\n  lines"])
def test_missing_empty_or_multiline_description_is_refused(
    tmp_path: Path, description: str | None
) -> None:
    path = write(tmp_path, agent_text(description=description))

    with pytest.raises(AgentFileError, match="description"):
        load_agent(path, DECLARATIONS)


@pytest.mark.parametrize(
    ("front_matter", "key"),
    [
        ("name: notes-assistant\nname: notes-assistant", "name"),
        ("appRoles: [admin]\nappRoles: [admin, user]", "appRoles"),
        ("tools: []\ntools: [edit_note]", "tools"),
        ("tools:\n  - read_note\ntools: [edit_note]", "tools"),
    ],
)
def test_duplicate_key_is_refused_and_named(tmp_path: Path, front_matter: str, key: str) -> None:
    path = front_matter_file(tmp_path, with_field_replaced(front_matter))

    with pytest.raises(AgentFileError, match=f"{key}: appears twice"):
        load_agent(path, DECLARATIONS)


@pytest.mark.parametrize(
    ("front_matter", "key"),
    [
        ("tools: *shared", "front matter"),
        ("description: |\n  two\n  lines", "description"),
        ("description: >\n  folded\n\n  paragraphs", "description"),
        ("tools:\n  nested: value", "tools"),
        ("color:\n  red: 1", "color"),
        ("description: Helps: with notes", "front matter"),
        ("description: 'unclosed", "front matter"),
        ('description: "bad \\q escape"', "front matter"),
        ("tools: [[read_note]]", "tools"),
        ("tools: {a: b}", "tools"),
        ("tools: [read_note", "front matter"),
        ("tools:", "tools"),
        ("\tdescription: tabbed", "front matter"),
        ("description: 123", "description"),
        ("description: true", "description"),
        ("description: null", "description"),
        ("tools: [read_note, 1]", "tools"),
        ("tools:\n  - read_note\n    - edit_note", "tools"),
    ],
    ids=lambda value: value.replace("\n", "|") if "\n" in value else value,
)
def test_front_matter_that_is_not_plain_text_is_refused_and_named(
    tmp_path: Path, front_matter: str, key: str
) -> None:
    path = front_matter_file(tmp_path, with_field_replaced(front_matter))

    with pytest.raises(AgentFileError, match=rf"notes-assistant\.md: .*{key}") as refusal:
        load_agent(path, DECLARATIONS)

    assert "appears twice" not in str(refusal.value)


@pytest.mark.parametrize(
    "roles", ["[admin, 1]", "[admin, true]", "[admin, null]", "\n  - admin\n    - user"]
)
def test_app_roles_that_are_not_text_or_are_misindented_are_refused(
    tmp_path: Path, roles: str
) -> None:
    path = write(tmp_path, agent_text(roles=roles))

    with pytest.raises(AgentFileError, match=r"notes-assistant\.md: appRoles"):
        load_agent(path, DECLARATIONS)


def test_name_that_is_a_number_is_refused(tmp_path: Path) -> None:
    path = write(tmp_path, agent_text(name="123"), stem="notes-assistant")

    with pytest.raises(AgentFileError, match=r"notes-assistant\.md: name"):
        load_agent(path, DECLARATIONS)


def test_nul_inside_quotes_is_refused_without_echoing_it(tmp_path: Path) -> None:
    path = write(tmp_path, agent_text(description='"secret\x00text"'))

    with pytest.raises(AgentFileError, match=r"notes-assistant\.md") as refusal:
        load_agent(path, DECLARATIONS)

    assert "secret" not in str(refusal.value)


@pytest.mark.parametrize("character", ["\x0b", "\x0c", "\x1c", "\x1d", "\x1e"])
def test_control_character_inside_quotes_is_refused_not_normalized(
    tmp_path: Path, character: str
) -> None:
    path = write(tmp_path, agent_text(description=f'"before{character}after"'))

    with pytest.raises(AgentFileError, match=r"notes-assistant\.md"):
        load_agent(path, DECLARATIONS)


@pytest.mark.parametrize(
    "fields",
    [
        {"extra": "description: sentinelvalue"},
        {"extra": "description: 'sentinelvalue"},
        {"model": "sentinelvalue"},
        {"description": "[sentinelvalue]"},
        {"extra": "voice: {windowBytes: sentinelvalue}"},
    ],
    ids=["duplicate-key", "syntax-error", "model-value", "non-string-description", "voice-window"],
)
def test_formatted_traceback_does_not_show_file_values(
    tmp_path: Path, fields: dict[str, str]
) -> None:
    path = write(tmp_path, agent_text(**fields))

    with pytest.raises(AgentFileError) as refusal:
        load_agent(path, DECLARATIONS)

    assert "sentinelvalue" not in "".join(traceback.format_exception(refusal.value))
    assert refusal.value.__cause__ is None
    assert refusal.value.__suppress_context__


def test_duplicate_key_message_does_not_echo_the_value(tmp_path: Path) -> None:
    path = write(tmp_path, agent_text(extra="description: topsecret"))

    with pytest.raises(AgentFileError, match="description: appears twice") as refusal:
        load_agent(path, DECLARATIONS)

    assert "topsecret" not in str(refusal.value)


def test_yaml_anchors_and_tags_are_read_as_yaml_reads_them(tmp_path: Path) -> None:
    agent = load(tmp_path, agent_text(tools="&shared [read_note]", description="!!str Helps"))

    assert (agent.tools, agent.description) == ((READ_NOTE,), "Helps")


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("Helps with notes", "Helps with notes"),
        ("'Helps: with notes'", "Helps: with notes"),
        ('"Helps: with # notes"', "Helps: with # notes"),
        ("'it''s'", "it's"),
        ('"say \\"hi\\" \\\\"', 'say "hi" \\'),
        ("a:b", "a:b"),
    ],
)
def test_scalars_are_plain_or_quoted_text(tmp_path: Path, description: str, expected: str) -> None:
    assert load(tmp_path, agent_text(description=description)).description == expected


@pytest.mark.parametrize(
    ("tools", "selected"),
    [
        ("[]", ()),
        ("[read_note]", (READ_NOTE,)),
        ("['edit_note', \"read_note\"]", (EDIT_NOTE, READ_NOTE)),
        ("[ read_note ,edit_note ]", (READ_NOTE, EDIT_NOTE)),
        ("\n  - read_note\n  - 'edit_note'", (READ_NOTE, EDIT_NOTE)),
    ],
    ids=["empty-flow", "one", "quoted-items", "loose-spacing", "block-list"],
)
def test_lists_are_flow_or_block(
    tmp_path: Path, tools: str, selected: tuple[ToolDeclaration, ...]
) -> None:
    assert load(tmp_path, agent_text(tools=tools)).tools == selected


def test_crlf_line_endings_load_like_lf(tmp_path: Path) -> None:
    text = agent_text(body="First.\n\nSecond.").replace("\n", "\r\n")

    agent = load(tmp_path, text.encode("utf-8"))

    assert (agent.name, agent.roles, agent.prompt) == (
        "notes-assistant",
        ["admin", "user"],
        "First.\n\nSecond.",
    )


@pytest.mark.parametrize(
    ("roles", "admin", "user"),
    [("[admin]", True, False), ("[admin, user]", True, True)],
)
def test_offered_to_follows_the_app_roles(
    tmp_path: Path, roles: str, admin: bool, user: bool
) -> None:
    agent = load(tmp_path, agent_text(roles=roles))

    assert (agent.offered_to("admin"), agent.offered_to("user")) == (admin, user)


@pytest.mark.parametrize("role", ["guest", ""])
def test_offered_to_refuses_a_role_outside_the_two(tmp_path: Path, role: str) -> None:
    agent = load(tmp_path, agent_text())

    with pytest.raises(ValueError, match="role"):
        agent.offered_to(role)  # type: ignore[arg-type]


def test_request_carries_the_files_prompt_model_roles_and_tools(tmp_path: Path) -> None:
    agent = load(tmp_path, agent_text())

    request = agent.request([TurnMessage(role=TurnRole.USER, text="hi")])

    assert request.agent == "notes-assistant"
    assert request.turn.system == PROMPT
    assert [message.text for message in request.turn.messages] == ["hi"]
    assert request.tools == (READ_NOTE, EDIT_NOTE)
    assert request.model == "text/opus"
    assert request.max_duration_seconds == 300
    assert request.roles == agent.roles


def test_request_is_frozen(tmp_path: Path) -> None:
    request: TurnRequest = load(tmp_path, agent_text()).request(
        [TurnMessage(role=TurnRole.USER, text="hi")]
    )

    with pytest.raises(ValidationError):
        request.model = "text/haiku"  # type: ignore[misc]


def test_request_model_is_the_override_when_given(tmp_path: Path) -> None:
    agent = load(tmp_path, agent_text())

    request = agent.request([TurnMessage(role=TurnRole.USER, text="hi")], model="text/haiku")

    assert request.model == "text/haiku"


PICTURE = "picture: {size: 1024x1024}"


def picture_text(**changes: str | None) -> str:
    fields = {"tools": "[]", "model": "image/draw", "extra": PICTURE} | changes
    return agent_text(**fields)


def test_a_picture_agents_override_is_an_image_name(tmp_path: Path) -> None:
    agent = load(tmp_path, picture_text())

    request = agent.request([TurnMessage(role=TurnRole.USER, text="a cat")], model="image/paint")

    assert request.model == "image/paint"


@pytest.mark.parametrize(
    ("agent_source", "override", "kind"),
    [
        (agent_text, "image/draw", "text"),
        (picture_text, "text/haiku", "image"),
    ],
    ids=["text-agent-image-name", "picture-agent-text-name"],
)
def test_request_refuses_an_override_of_the_wrong_kind(
    tmp_path: Path, agent_source, override: str, kind: str
) -> None:
    agent = load(tmp_path, agent_source())
    messages = [TurnMessage(role=TurnRole.USER, text="hi")]

    with pytest.raises(ValueError, match=f"must start with {kind}/"):
        agent.request(messages, model=override)


def test_request_refuses_a_malformed_model_override(tmp_path: Path) -> None:
    agent = load(tmp_path, agent_text())

    messages = [TurnMessage(role=TurnRole.USER, text="hi")]

    with pytest.raises(ValueError, match="model"):
        agent.request(messages, model="Text/Opus")


def test_request_of_an_agent_without_tools_names_none(tmp_path: Path) -> None:
    agent = load(tmp_path, agent_text(tools="[]"))

    assert agent.request([TurnMessage(role=TurnRole.USER, text="hi")]).tools == ()


def test_request_without_messages_is_refused(tmp_path: Path) -> None:
    agent = load(tmp_path, agent_text())

    with pytest.raises(ValidationError):
        agent.request([])


def test_loaded_definition_is_frozen(tmp_path: Path) -> None:
    agent: AgentDefinition = load(tmp_path, agent_text())

    with pytest.raises(ValidationError):
        agent.name = "other"  # type: ignore[misc]


def test_a_picture_agent_carries_its_size_into_its_request(tmp_path: Path) -> None:
    agent = load(tmp_path, picture_text())

    request = agent.request([TurnMessage(role=TurnRole.USER, text="a cat")])

    assert (agent.picture_size, request.picture_size, request.model) == (
        "1024x1024",
        "1024x1024",
        "image/draw",
    )


def test_a_text_agent_has_no_picture_size(tmp_path: Path) -> None:
    agent = load(tmp_path, agent_text())

    assert agent.picture_size is None
    assert agent.request([TurnMessage(role=TurnRole.USER, text="hi")]).picture_size is None


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"model": "text/opus"}, "model"),
        ({"extra": ""}, "model"),
        ({"tools": "[read_note]"}, "tools"),
        ({"max_tool_calls": "5"}, "maxToolCalls"),
        ({"extra": f"{PICTURE}\nvoice: {{windowBytes: 4096}}"}, "voice"),
    ],
)
def test_a_picture_file_that_does_not_fit_a_picture_agent_is_refused_and_named(
    tmp_path: Path, changes: dict[str, str], field: str
) -> None:
    text = picture_text(**changes)

    with pytest.raises(AgentFileError, match=rf"notes-assistant\.md: {field}"):
        load(tmp_path, text)


@pytest.mark.parametrize(
    "size",
    [
        "1024",
        "1024x",
        "x1024",
        "0x10",
        "10x0",
        "01x10",
        "1024X1024",
        "1024 x 1024",
        "-1x5",
        "'1x2x3'",
        "1.5x2",
        "1٠x1",
        "7",
        "''",
    ],
)
def test_a_malformed_picture_size_is_refused_and_named(tmp_path: Path, size: str) -> None:
    text = picture_text(extra=f"picture: {{size: {size}}}")

    with pytest.raises(AgentFileError, match="picture"):
        load(tmp_path, text)


@pytest.mark.parametrize(
    "picture", ["picture: {}", "picture: {size: 1x1, quality: high}", "picture: 1x1"]
)
def test_a_picture_key_that_is_not_exactly_a_size_is_refused_and_named(
    tmp_path: Path, picture: str
) -> None:
    text = picture_text(extra=picture)

    with pytest.raises(AgentFileError, match="picture"):
        load(tmp_path, text)


@pytest.mark.parametrize(
    ("text", "changes"),
    [
        (agent_text(), {"max_tool_calls": None}),
        (picture_text(), {"voice_window_bytes": 4096}),
    ],
)
def test_a_definition_that_breaks_a_file_rule_is_refused_where_it_is_built(
    tmp_path: Path, text: str, changes: dict[str, object]
) -> None:
    agent = load(tmp_path, text)

    with pytest.raises(ValidationError):
        AgentDefinition(**{**dict(agent), **changes})


def test_a_voice_agent_carries_its_window_and_any_other_agent_none(tmp_path: Path) -> None:
    voiced = load(tmp_path, agent_text(extra="voice: {windowBytes: 4096}"))
    silent = load(tmp_path, agent_text())

    assert (voiced.voice_window_bytes, silent.voice_window_bytes) == (4096, None)


@pytest.mark.parametrize(
    "voice",
    [
        "voice: {}",
        "voice: {windowBytes: 0}",
        "voice: {windowBytes: -1}",
        "voice: {windowBytes: '10'}",
        "voice: {windowBytes: 1.5}",
        "voice: {windowBytes: true}",
        "voice: {windowBytes: 10, unit: tokens}",
        "voice: 10",
    ],
)
def test_a_voice_section_that_is_not_one_positive_window_is_refused_and_named(
    tmp_path: Path, voice: str
) -> None:
    text = agent_text(extra=voice)

    with pytest.raises(AgentFileError, match=r"notes-assistant\.md: voice"):
        load(tmp_path, text)


@pytest.mark.parametrize(
    "turn_messages",
    [
        [],
        [TurnMessage(role=TurnRole.USER, text="a"), TurnMessage(role=TurnRole.USER, text="b")],
        [TurnMessage(role=TurnRole.ASSISTANT, text="a")],
    ],
)
def test_a_picture_request_holds_exactly_one_user_message(
    tmp_path: Path, turn_messages: list[TurnMessage]
) -> None:
    agent = load(tmp_path, picture_text())

    with pytest.raises(ValueError, match="message"):
        agent.request(turn_messages)


def test_a_picture_request_with_tools_is_refused() -> None:
    turn = Turn(system="s", messages=[TurnMessage(role=TurnRole.USER, text="a")])

    with pytest.raises(ValueError, match="tools"):
        TurnRequest(
            agent="painter",
            turn=turn,
            roles=["admin"],
            tools=(READ_NOTE,),
            model="image/draw",
            max_duration_seconds=30,
            max_tool_calls=5,
            picture_size="1x1",
        )
