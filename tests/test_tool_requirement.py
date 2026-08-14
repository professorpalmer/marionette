"""Hermetic tests for SoftToolRequirement and MICRO/STANDARD schema compaction."""
from __future__ import annotations

from harness.mcp_client import McpTool
from harness.task_profile import DEEP, MICRO, STANDARD
from harness.tool_discovery import ToolCatalog
from harness.tool_requirement import SoftToolRequirement


def test_should_remind_micro_with_edits_no_command():
    assert SoftToolRequirement.should_remind_verify(MICRO, 1, False, 0) is True
    assert SoftToolRequirement.should_remind_verify(STANDARD, 2, False, 2) is True


def test_should_remind_false_when_ran_command_or_no_edits():
    assert SoftToolRequirement.should_remind_verify(MICRO, 1, True, 0) is False
    assert SoftToolRequirement.should_remind_verify(MICRO, 0, False, 0) is False


def test_should_remind_false_for_deep_or_none_or_exhausted():
    assert SoftToolRequirement.should_remind_verify(DEEP, 1, False, 0) is False
    assert SoftToolRequirement.should_remind_verify(None, 1, False, 0) is False
    assert SoftToolRequirement.should_remind_verify(MICRO, 1, False, 3) is False
    assert SoftToolRequirement.should_remind_verify(MICRO, 1, False, 4) is False


def test_remind_message_asks_to_verify():
    msg = SoftToolRequirement.remind_message()
    assert isinstance(msg, str) and msg.strip()
    lowered = msg.lower()
    assert "verify" in lowered or "test" in lowered
    assert "finish" in lowered or "before" in lowered


def test_should_escalate_after_three_reminds_unverified():
    assert SoftToolRequirement.should_escalate_unverified(MICRO, 1, False, 3) is True
    assert SoftToolRequirement.should_escalate_unverified(STANDARD, 2, False, 5) is True


def test_should_escalate_false_when_command_run_or_no_edits_or_under_budget():
    assert SoftToolRequirement.should_escalate_unverified(MICRO, 1, True, 3) is False
    assert SoftToolRequirement.should_escalate_unverified(MICRO, 0, False, 3) is False
    assert SoftToolRequirement.should_escalate_unverified(MICRO, 1, False, 2) is False


def test_micro_standard_compact_long_catalog_description():
    long_desc = (
        "First sentence about creating GitHub issues with a title and body. "
        + ("Additional trailing detail about labels milestones assignees and projects. " * 8)
    )
    assert len(long_desc) > 160

    tool = McpTool(
        server="github",
        name="create_issue",
        description=long_desc,
        input_schema={"type": "object", "properties": {}},
    )

    for profile in (MICRO, STANDARD):
        catalog = ToolCatalog()
        catalog.refresh(mcp_tools=[tool], profile=profile)
        entry = next(e for e in catalog._entries.values() if e.source == "mcp")
        assert len(entry.description) < len(long_desc)
        assert len(entry.description) <= 160
        assert entry.description.startswith("First sentence about creating GitHub issues")
        assert "Additional trailing detail" not in entry.description
        schema_desc = entry.schema_entry["function"]["description"]
        assert schema_desc == entry.description


def test_deep_and_none_keep_full_catalog_description():
    long_desc = (
        "First sentence about creating GitHub issues with a title and body. "
        "Additional trailing detail about labels milestones assignees and projects."
    )
    tool = McpTool(
        server="github",
        name="create_issue",
        description=long_desc,
        input_schema={"type": "object", "properties": {}},
    )

    for profile in (DEEP, None):
        catalog = ToolCatalog()
        catalog.refresh(mcp_tools=[tool], profile=profile)
        entry = next(e for e in catalog._entries.values() if e.source == "mcp")
        assert entry.description == long_desc
        assert "Additional trailing detail" in entry.description
