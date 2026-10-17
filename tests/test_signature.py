"""Shape-based labelling: no vendor table, and unknown is not a candidate.

The safety property that matters most here: a process whose command line cannot
be identified as a server must be left alone, because grouping a wrong process
into a session means a live session loses a server it was using.
"""

import json
import tempfile
import unittest
from pathlib import Path

from mcp_reaper.signature import classify, derive, load_rules


class DeriveTest(unittest.TestCase):
    def test_npx_scoped_package_uses_the_name_not_the_scope(self):
        self.assertEqual(derive("npx -y @upstash/context7-mcp"), "context7-mcp")

    def test_npm_exec_carries_the_package_name_in_the_next_token(self):
        # ``npm exec`` labels as "npm" unless the subcommand is understood, and
        # then every npm-launched server in a session merges into one group
        self.assertEqual(derive("npm exec @upstash/context7-mcp"), "context7-mcp")
        self.assertEqual(derive("pnpm dlx some-mcp-server --stdio"), "some-mcp-server")

    def test_uvx_from_package(self):
        self.assertEqual(derive("uvx --from some-mcp-server server"), "some-mcp-server")

    def test_generic_script_stem_climbs_out_of_build_directories(self):
        # dist/index.js is the same filename for every npm package, so the label
        # has to come from the package directory or every server groups together
        self.assertEqual(derive("node /opt/tools/context7-mcp/dist/index.js"), "context7-mcp")

    def test_named_script_uses_its_own_stem(self):
        self.assertEqual(derive("node /opt/mcp/github-mcp-server.js"), "github-mcp-server")

    def test_python_entry(self):
        self.assertEqual(derive("python3 /venv/bin/mcp_server.py"), "mcp_server")

    def test_bare_executable(self):
        self.assertEqual(derive("/usr/local/bin/github-mcp-server --stdio"), "github-mcp-server")

    def test_quoted_paths_with_spaces(self):
        self.assertEqual(derive('node "/Applications/My App/mcp/index.js"'), "mcp")


class ClassifyTest(unittest.TestCase):
    def test_unrelated_process_is_not_a_candidate(self):
        self.assertIsNone(classify("/usr/sbin/syslogd -x"))
        self.assertIsNone(classify("node server.js --port 8080"))

    def test_mcp_marker_admits_a_process(self):
        self.assertIsNotNone(classify("node /opt/foo-mcp/dist/index.js"))

    def test_marker_is_configurable(self):
        self.assertIsNotNone(classify("node /opt/cua_node/bin/server.mjs", marker="cua_node"))
        self.assertIsNone(classify("node /opt/cua_node/bin/server.mjs"))

    def test_rule_overrides_shape_and_admits_unmarked_commands(self):
        rules = load_rules(self._write([{"name": "cua-server", "contains": ["cua_node/bin/node", "server.mjs"]}]))
        self.assertEqual(classify("/opt/cua_node/bin/node ./server.mjs", rules), "cua-server")

    def test_rules_are_matched_case_insensitively(self):
        rules = load_rules(self._write([{"name": "GitHub", "contains": ["GITHUB-MCP-SERVER"]}]))
        self.assertEqual(classify("node /opt/github-mcp-server/index.js", rules), "GitHub")

    def _write(self, entries):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"rules": entries}, handle)
        handle.close()
        self.addCleanup(Path(handle.name).unlink)
        return handle.name


class LoadRulesTest(unittest.TestCase):
    def test_none_path_yields_no_rules(self):
        self.assertEqual(load_rules(None), [])

    def test_accepts_bare_list(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump([{"name": "x", "contains": ["x-mcp"]}], handle)
        self.addCleanup(Path(handle.name).unlink)
        rules = load_rules(handle.name)
        self.assertEqual([r.name for r in rules], ["x"])

    def test_incomplete_rule_is_dropped(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump({"rules": [{"name": "no-pattern"}, {"name": "ok", "contains": ["ok-mcp"]}]}, handle)
        self.addCleanup(Path(handle.name).unlink)
        self.assertEqual([r.name for r in load_rules(handle.name)], ["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
