"""
Focused unit tests for the optional `repo` (target_dir) parameter added to
`run_implement` and `run_parallel`.

Covers, hermetically and with no network / no subprocess dispatch:
  (a) PilotAction carries the repo field.
  (b) build_tools_schema exposes `repo` as an OPTIONAL string property on
      both run_implement and run_parallel (present in properties, absent
      from required).
  (c) The tool-call arg-mapping (_tool_name_to_action / parse_tool_calls)
      maps `args["repo"]` (and the alias `target_dir`) onto the action.

Stdlib-only, deterministic. No emojis.
"""

import json
import unittest

from harness.pilot import (
    PilotAction,
    build_tools_schema,
    _tool_name_to_action,
    parse_tool_calls,
)


def _tool_by_name(schema, name):
    for entry in schema:
        fn = entry.get("function") or {}
        if fn.get("name") == name:
            return fn
    return None


class PilotActionRepoFieldTests(unittest.TestCase):
    def test_pilot_action_has_repo_field_default_empty(self):
        act = PilotAction(kind="run_implement", goal="do the thing")
        self.assertTrue(hasattr(act, "repo"))
        self.assertEqual(act.repo, "")

    def test_pilot_action_carries_explicit_repo(self):
        act = PilotAction(
            kind="run_implement",
            goal="do the thing",
            repo="/tmp/some/repo",
        )
        self.assertEqual(act.repo, "/tmp/some/repo")

    def test_pilot_action_repo_on_run_parallel(self):
        act = PilotAction(
            kind="run_parallel",
            goals=["a", "b"],
            repo="/tmp/other/repo",
        )
        self.assertEqual(act.repo, "/tmp/other/repo")


class ToolsSchemaRepoPropertyTests(unittest.TestCase):
    def test_run_implement_exposes_optional_repo(self):
        schema = build_tools_schema()
        fn = _tool_by_name(schema, "run_implement")
        self.assertIsNotNone(fn, "run_implement tool missing from schema")
        params = fn.get("parameters") or {}
        props = params.get("properties") or {}
        self.assertIn("repo", props, "run_implement should expose 'repo' property")
        repo_prop = props["repo"]
        self.assertEqual(repo_prop.get("type"), "string")
        # Description must indicate it's a DIFFERENT repo / defaults to workspace.
        desc = (repo_prop.get("description") or "").lower()
        self.assertIn("different", desc)
        self.assertIn("git repository", desc)
        # Must NOT be listed as required.
        required = params.get("required") or []
        self.assertNotIn("repo", required)
        # Sanity: goal remains required.
        self.assertIn("goal", required)

    def test_run_parallel_exposes_optional_repo(self):
        schema = build_tools_schema()
        fn = _tool_by_name(schema, "run_parallel")
        self.assertIsNotNone(fn, "run_parallel tool missing from schema")
        params = fn.get("parameters") or {}
        props = params.get("properties") or {}
        self.assertIn("repo", props, "run_parallel should expose 'repo' property")
        self.assertEqual(props["repo"].get("type"), "string")
        required = params.get("required") or []
        self.assertNotIn("repo", required)
        # Sanity: goals remains required.
        self.assertIn("goals", required)

    def test_schema_is_json_serializable(self):
        # Deterministic surface: the whole schema must survive json.dumps so
        # both providers (native tool-call flavours) can transmit it verbatim.
        schema = build_tools_schema()
        json.dumps(schema)


class ArgMappingRepoTests(unittest.TestCase):
    def test_direct_mapping_run_implement_repo(self):
        act = _tool_name_to_action(
            "run_implement",
            {"goal": "add tests", "repo": "/tmp/target/repo"},
            tool_call_id="tc-1",
        )
        self.assertEqual(act.kind, "run_implement")
        self.assertEqual(act.goal, "add tests")
        self.assertEqual(act.repo, "/tmp/target/repo")
        self.assertEqual(act.tool_call_id, "tc-1")

    def test_direct_mapping_run_parallel_repo(self):
        act = _tool_name_to_action(
            "run_parallel",
            {"goals": ["one", "two"], "repo": "/tmp/target/repo"},
            tool_call_id="tc-2",
        )
        self.assertEqual(act.kind, "run_parallel")
        self.assertEqual(act.goals, ["one", "two"])
        self.assertEqual(act.repo, "/tmp/target/repo")

    def test_target_dir_alias_mapped_to_repo(self):
        # Some models emit the parameter under the more descriptive
        # `target_dir` alias; the mapper must accept it.
        act = _tool_name_to_action(
            "run_implement",
            {"goal": "x", "target_dir": "/tmp/alias/repo"},
        )
        self.assertEqual(act.repo, "/tmp/alias/repo")

    def test_missing_repo_defaults_empty(self):
        act = _tool_name_to_action(
            "run_implement",
            {"goal": "no repo"},
        )
        self.assertEqual(act.repo, "")

    def test_repo_ignored_for_non_dispatch_kinds(self):
        # `repo` is only meaningful for run_implement / run_parallel. Other
        # tools must not silently pick it up (avoids leaking cross-tool args).
        act = _tool_name_to_action(
            "read_file",
            {"path": "README.md", "repo": "/tmp/should/not/apply"},
        )
        self.assertEqual(act.repo, "")

    def test_parse_tool_calls_end_to_end(self):
        # Same shape the providers hand us: function.arguments as a JSON string.
        tool_calls = [{
            "id": "tc-42",
            "function": {
                "name": "run_implement",
                "arguments": json.dumps({
                    "goal": "port the thing",
                    "repo": "/tmp/another/repo",
                }),
            },
        }]
        actions = parse_tool_calls(tool_calls)
        self.assertEqual(len(actions), 1)
        act = actions[0]
        self.assertEqual(act.kind, "run_implement")
        self.assertEqual(act.goal, "port the thing")
        self.assertEqual(act.repo, "/tmp/another/repo")


if __name__ == "__main__":
    unittest.main()
