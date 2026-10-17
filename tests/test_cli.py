import io
import json
import unittest
from unittest import mock

from mcp_reaper import cli
from mcp_reaper.proctable import Row
from mcp_reaper.reap import build_plan

CLIENT = "codex-cli --session"


def tree(leaks=3):
    spec = {1: (0, 99999, "init"), 2: (1, 9999, CLIENT)}
    for index in range(leaks):
        spec[100 + index] = (2, 1000 + 100 * (leaks - 1 - index), "node /opt/context7-mcp/dist/index.js")
    return {pid: Row(pid, ppid, age, cmd, 501) for pid, (ppid, age, cmd) in spec.items()}


class ExitCodeTest(unittest.TestCase):
    """The wrapper contract: a scheduled job decides on the code, never on text."""

    def run_cli(self, argv, table):
        out = io.StringIO()
        with mock.patch.object(cli, "read_table", return_value=table), \
             mock.patch("sys.stdout", out), mock.patch("sys.stderr", io.StringIO()):
            return cli.main(argv), out.getvalue()

    def test_plan_reports_eligible_as_exit_one(self):
        code, _ = self.run_cli(["plan"], tree(3))
        self.assertEqual(code, 1)

    def test_plan_with_nothing_to_do_is_exit_zero(self):
        code, _ = self.run_cli(["plan"], tree(1))
        self.assertEqual(code, 0)

    def test_reap_refuses_without_yes(self):
        code, _ = self.run_cli(["reap"], tree(3))
        self.assertEqual(code, 1)

    def test_reap_with_yes_signals_and_returns_two(self):
        sent = []
        with mock.patch.object(cli, "read_table", return_value=tree(3)), \
             mock.patch.object(cli, "identity", return_value="same"), \
             mock.patch("sys.stdout", io.StringIO()), \
             mock.patch("mcp_reaper.reap.os.kill", side_effect=lambda pid, sig: sent.append(pid)):
            code = cli.main(["reap", "--yes"])
        self.assertEqual(code, 2)
        self.assertEqual(sorted(sent), [100, 101])

    def test_reap_without_yes_never_signals(self):
        sent = []
        with mock.patch.object(cli, "read_table", return_value=tree(3)), \
             mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()), \
             mock.patch("mcp_reaper.reap.os.kill", side_effect=lambda pid, sig: sent.append(pid)):
            code = cli.main(["reap"])
        self.assertEqual(code, 1)
        self.assertEqual(sent, [])

    def test_max_guard_refuses_a_bulk_plan(self):
        buffer = io.StringIO()
        with mock.patch.object(cli, "read_table", return_value=tree(6)), \
             mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", buffer):
            code = cli.main(["reap", "--yes", "--max", "2"])
        self.assertEqual(code, 4)
        self.assertIn("refusing", buffer.getvalue())

    def test_keep_below_one_is_usage_error(self):
        code, _ = self.run_cli(["plan", "--keep", "0"], tree(3))
        self.assertEqual(code, 4)


class OutputTest(unittest.TestCase):
    def test_plan_json_lists_eligible_and_kept(self):
        with mock.patch.object(cli, "read_table", return_value=tree(3)):
            buffer = []
            with mock.patch("builtins.print", side_effect=buffer.append):
                cli.main(["plan", "--json"])
        payload = json.loads("\n".join(str(line) for line in buffer))
        self.assertEqual(payload["eligible"], 2)
        self.assertEqual(payload["keep"], 1)
        group = payload["groups"][0]
        self.assertEqual(group["type"], "context7-mcp")
        self.assertEqual([k["pid"] for k in group["kept"]], [102])
        self.assertEqual(sorted(k["pid"] for k in group["eligible"]), [100, 101])

    def test_explain_shows_session_root(self):
        with mock.patch.object(cli, "read_table", return_value=tree(2)):
            buffer = []
            with mock.patch("builtins.print", side_effect=buffer.append):
                cli.main(["explain", "--json"])
        rows = json.loads("\n".join(str(line) for line in buffer))
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["session_root"] == 2 for row in rows))

    def test_human_output_never_claims_to_have_killed_anything(self):
        with mock.patch.object(cli, "read_table", return_value=tree(3)):
            buffer = []
            with mock.patch("builtins.print", side_effect=buffer.append):
                cli.main(["plan"])
        text = "\n".join(str(line) for line in buffer)
        self.assertIn("would SIGTERM", text)
        self.assertNotIn("SIGTERM 100", text)


class AmbiguityTest(unittest.TestCase):
    def test_orphans_are_reported_but_not_planned(self):
        spec = {100: (1, 5000, "node x-mcp.js"), 101: (1, 6000, "node x-mcp.js")}
        table = {pid: Row(pid, ppid, age, cmd, 501) for pid, (ppid, age, cmd) in spec.items()}
        plan = build_plan(table, self_pid=999)
        self.assertEqual(plan.reap, [])
        self.assertEqual(len(plan.deferred_ambiguous), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
