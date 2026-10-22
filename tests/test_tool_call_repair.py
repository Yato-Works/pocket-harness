"""Tests for the C3 ToolCallRepair."""

from __future__ import annotations

import json

from pocket_harness.components.tool_call_repair import (
    RepairFailure,
    ToolCallRepairer,
    parse_arguments,
)

READ_FILE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "limit": {"type": "integer"},
        "recursive": {"type": "boolean"},
        "mode": {"type": "string", "enum": ["read", "write"]},
    },
    "required": ["path"],
}


def repairer(**kwargs) -> ToolCallRepairer:
    """Return a repairer with the read_file tool registered."""
    return ToolCallRepairer({"read_file": READ_FILE_SCHEMA}, **kwargs)


def test_valid_call_passes_unchanged() -> None:
    """A valid call is accepted without modification."""
    outcome = repairer().repair("read_file", '{"path": "a.py"}')
    assert outcome.ok
    assert not outcome.changed
    assert outcome.arguments == {"path": "a.py"}


def test_code_fenced_arguments_are_parsed() -> None:
    """A fenced JSON block is unwrapped."""
    outcome = repairer().repair("read_file", '```json\n{"path": "a.py"}\n```')
    assert outcome.ok
    assert outcome.arguments == {"path": "a.py"}


def test_trailing_comma_is_tolerated() -> None:
    """A trailing comma is removed."""
    outcome = repairer().repair("read_file", '{"path": "a.py",}')
    assert outcome.ok
    assert outcome.arguments == {"path": "a.py"}


def test_single_quoted_arguments_are_tolerated() -> None:
    """Python-style single quotes are converted to JSON double quotes."""
    outcome = repairer().repair("read_file", "{'path': 'a.py'}")
    assert outcome.ok
    assert outcome.arguments == {"path": "a.py"}


def test_invalid_json_is_reported_not_guessed() -> None:
    """Unparseable arguments fail with a parse error class."""
    outcome = repairer().repair("read_file", "{path: ")
    assert not outcome.ok
    assert outcome.error_class == "M.parse"


def test_unknown_tool_is_rejected() -> None:
    """A tool that is not in the client list is rejected."""
    outcome = repairer().repair("delete_everything", "{}")
    assert not outcome.ok
    assert outcome.error_class == "M.unknown_tool"


def test_case_insensitive_tool_name_is_resolved() -> None:
    """A differently-cased tool name resolves to the canonical one."""
    outcome = repairer().repair("READ_FILE", '{"path": "a.py"}')
    assert outcome.ok
    assert outcome.tool_name == "read_file"


def test_alias_table_is_used() -> None:
    """An explicit alias resolves to the canonical tool."""
    outcome = repairer(aliases={"read": "read_file"}).repair("read", '{"path": "a.py"}')
    assert outcome.ok
    assert outcome.tool_name == "read_file"


def test_fuzzy_matching_is_off_by_default() -> None:
    """A near-miss tool name is rejected when fuzzy matching is disabled."""
    outcome = repairer().repair("readfile", '{"path": "a.py"}')
    assert not outcome.ok
    assert outcome.error_class == "M.unknown_tool"


def test_fuzzy_matching_can_be_enabled() -> None:
    """Fuzzy matching resolves an unambiguous near-miss when enabled."""
    outcome = repairer(allow_fuzzy_tool_name=True).repair("readfile", '{"path": "a.py"}')
    assert outcome.ok
    assert outcome.tool_name == "read_file"


def test_fuzzy_matching_is_refused_for_destructive_tools() -> None:
    """A destructive tool is never matched by a fuzzy name."""
    outcome = repairer(allow_fuzzy_tool_name=True, destructive_tools={"read_file"}).repair(
        "readfile", '{"path": "a.py"}'
    )
    assert not outcome.ok


def test_missing_required_argument_is_rejected() -> None:
    """A missing required argument is a schema error, not a guessable default."""
    outcome = repairer().repair("read_file", "{}")
    assert not outcome.ok
    assert outcome.error_class == "M.schema_invalid"


def test_string_integer_is_converted_losslessly() -> None:
    """A numeric string becomes an integer."""
    outcome = repairer().repair("read_file", '{"path": "a.py", "limit": "10"}')
    assert outcome.ok
    assert outcome.arguments["limit"] == 10
    assert outcome.changed


def test_string_boolean_is_converted_losslessly() -> None:
    """A boolean string becomes a boolean."""
    outcome = repairer().repair("read_file", '{"path": "a.py", "recursive": "true"}')
    assert outcome.ok
    assert outcome.arguments["recursive"] is True


def test_enum_case_is_normalized() -> None:
    """An enum value with different case is normalized."""
    outcome = repairer().repair("read_file", '{"path": "a.py", "mode": "READ"}')
    assert outcome.ok
    assert outcome.arguments["mode"] == "read"


def test_invalid_enum_value_is_not_guessed() -> None:
    """An unknown enum value is rejected rather than coerced."""
    outcome = repairer().repair("read_file", '{"path": "a.py", "mode": "append"}')
    assert not outcome.ok
    assert outcome.error_class == "M.schema_invalid"


def test_destructive_tool_gets_no_argument_conversion() -> None:
    """A destructive tool is not repaired beyond syntax level."""
    outcome = repairer(destructive_tools={"read_file"}).repair(
        "read_file", '{"path": "a.py", "limit": "10"}'
    )
    assert not outcome.ok
    assert outcome.error_class == "M.schema_invalid"


def test_diff_is_recorded() -> None:
    """The structured diff reports the changed keys."""
    outcome = repairer().repair("read_file", '{"path": "a.py", "limit": "10"}')
    assert "limit" in outcome.diff["changed_keys"]


def test_hashes_differ_only_when_content_differs() -> None:
    """The original and repaired hashes are equal for an unmodified call."""
    outcome = repairer().repair("read_file", '{"path": "a.py"}')
    assert outcome.original_hash == outcome.repaired_hash


def test_parse_arguments_rejects_non_objects() -> None:
    """A JSON array is not a valid argument object."""
    try:
        parse_arguments("[1, 2, 3]")
    except RepairFailure as exc:
        assert "JSON object" in str(exc)
    else:
        raise AssertionError("expected a RepairFailure")


def test_parse_arguments_accepts_empty() -> None:
    """Empty arguments parse to an empty mapping."""
    assert parse_arguments("") == {}
    assert parse_arguments("   ") == {}


def test_json_schema_is_validated_against_the_client_schema() -> None:
    """An additional property is rejected when the schema forbids it."""
    strict = ToolCallRepairer(
        {
            "read_file": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "additionalProperties": False,
            }
        }
    )
    outcome = strict.repair("read_file", '{"path": "a.py", "extra": 1}')
    assert not outcome.ok
    assert outcome.error_class == "M.schema_invalid"


def test_repaired_output_is_valid_json() -> None:
    """The repaired arguments round-trip through JSON."""
    outcome = repairer().repair("read_file", "{'path': 'a.py', 'limit': '10',}")
    assert outcome.ok
    assert json.loads(outcome.repaired_arguments_text)["limit"] == 10

