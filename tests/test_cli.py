import io
import json
import unittest
from unittest import mock

from mcp_reaper import cli
from mcp_reaper.proctable import Row
from mcp_reaper.reap import build_plan, execute, session_root

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


class SessionHostBoundaryTest(unittest.TestCase):
    """A prefix match here merges two sessions' groups and kills a live server."""

    def rows(self):
        return {
            1: Row(1, 0, 9999, "/usr/local/bin/gooseberry-mcp-server --stdio", 501),
            2: 2 and Row(2, 1, 9000, "/usr/local/bin/goose --session", 501),
            3: Row(3, 1, 9000, "/usr/local/bin/kimono-assistant", 501),
        }

    def test_unrelated_binary_is_not_treated_as_a_session_host(self):
        table = self.rows()
        # gooseberry must NOT be a host, or it would host itself and the real
        # goose session's servers would land in the wrong group
        self.assertIsNone(session_root(1, table, ("goose",)))
        self.assertEqual(session_root(2, table, ("goose",)), 2)
        self.assertIsNone(session_root(3, table, ("kimi",)))

    def test_suffixed_client_binary_is_still_a_host(self):
        table = {5: Row(5, 1, 999, "/usr/bin/codex-cli --session", 501),
                 6: Row(6, 5, 100, "node a-mcp.js", 501)}
        self.assertEqual(session_root(6, table, ("codex",)), 5)

    def test_explain_and_plan_agree_on_host_exclusion(self):
        # `explain` is what an operator reads before trusting `reap`; listing the
        # session client as a candidate there while the planner protects it would
        # make the two commands contradict each other
        spec = {2: (1, 9999, "codex-cli"), 10: (2, 5000, "node a-mcp.js"), 11: (2, 6000, "node a-mcp.js")}
        table = {pid: Row(pid, ppid, age, cmd, 501) for pid, (ppid, age, cmd) in spec.items()}
        plan = build_plan(table, self_pid=999)
        # 11 has the larger age_sec, so it is the older copy and 10 is kept
        self.assertEqual([t.pid for t in plan.reap], [11])
        with mock.patch.object(cli, "read_table", return_value=table):
            buffer = []
            with mock.patch("builtins.print", side_effect=buffer.append):
                cli.main(["explain", "--json"])
        listed = json.loads("".join(str(line) for line in buffer))
        self.assertEqual(sorted(row["pid"] for row in listed), [10, 11])
        self.assertNotIn(2, [row["pid"] for row in listed], "the client itself is never a candidate")


class MarkerGuardTest(unittest.TestCase):
    def test_empty_marker_is_refused(self):
        # --marker "" would make every process a candidate: the one setting that
        # turns the safety model off must not be reachable
        with mock.patch("sys.stderr", io.StringIO()) as err, mock.patch("sys.stdout", io.StringIO()):
            code = cli.main(["plan", "--marker", ""])
        self.assertEqual(code, 4)
        self.assertIn("at least 2 characters", err.getvalue())

    def test_single_character_marker_is_refused(self):
        with mock.patch("sys.stderr", io.StringIO()), mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(cli.main(["plan", "--marker", "x"]), 4)


class ArgvPrivacyTest(unittest.TestCase):
    def test_token_arguments_are_masked_in_output(self):
        from mcp_reaper.reap import redact_command
        raw = "node /opt/foo-mcp/index.js --token ghp_abcdefghijklmnop --api-key=sk-abcdef123456"
        masked = redact_command(raw)
        self.assertNotIn("ghp_abcdefghijklmnop", masked)
        self.assertNotIn("sk-abcdef123456", masked)
        self.assertIn("--token=[redacted]", masked)
        self.assertIn("/opt/foo-mcp/index.js", masked, "the identifying part must stay visible")

    def test_plan_json_carries_no_token(self):
        spec = {2: (1, 9999, "codex-cli"), 10: (2, 5000, "node a-mcp.js --token secret123456"),
                11: (2, 6000, "node a-mcp.js --token secret123456")}
        table = {pid: Row(pid, ppid, age, cmd, 501) for pid, (ppid, age, cmd) in spec.items()}
        with mock.patch.object(cli, "read_table", return_value=table):
            buffer = []
            with mock.patch("builtins.print", side_effect=buffer.append):
                cli.main(["plan", "--json"])
        text = "".join(str(line) for line in buffer)
        self.assertNotIn("secret123456", text)
        self.assertIn("a-mcp", text)


class RulesFileFailureTest(unittest.TestCase):
    def test_bad_rules_file_exits_four_not_one(self):
        # exit 1 is the wrapper's signal that something should be reaped; a broken
        # config must never be read as that
        import tempfile
        from pathlib import Path
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        handle.write(json.dumps({"rules": [{"contains": ["x"]}]}))
        handle.close()
        self.addCleanup(Path(handle.name).unlink)
        with mock.patch.object(cli, "read_table", return_value={}), \
             mock.patch("sys.stderr", io.StringIO()), mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(cli.main(["plan", "--rules", handle.name]), 4)


class AmbiguousFlagTest(unittest.TestCase):
    def test_reap_ambiguous_flag_is_wired(self):
        spec = {100: (1, 4000, "node x-mcp.js"), 101: (1, 3000, "node x-mcp.js")}
        table = {pid: Row(pid, ppid, age, cmd, 501) for pid, (ppid, age, cmd) in spec.items()}
        sent = []
        with mock.patch.object(cli, "read_table", return_value=table), \
             mock.patch.object(cli, "identity", return_value="same"), \
             mock.patch("mcp_reaper.reap.os.kill", side_effect=lambda pid, sig: sent.append(pid)), \
             mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(cli.main(["reap", "--yes", "--reap-ambiguous"]), 2)
        self.assertEqual(sent, [100])

    def test_without_the_flag_nothing_is_signalled(self):
        spec = {100: (1, 4000, "node x-mcp.js"), 101: (1, 3000, "node x-mcp.js")}
        table = {pid: Row(pid, ppid, age, cmd, 501) for pid, (ppid, age, cmd) in spec.items()}
        sent = []
        with mock.patch.object(cli, "read_table", return_value=table), \
             mock.patch("mcp_reaper.reap.os.kill", side_effect=lambda pid, sig: sent.append(pid)), \
             mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(cli.main(["reap", "--yes"]), 0)
        self.assertEqual(sent, [])


class UidForwardTest(unittest.TestCase):
    def test_revalidation_reads_the_same_uid_as_the_plan(self):
        # dropping uid here meant every target looked "gone" when planning for
        # another uid, so the reaper reported doing nothing while claiming success
        spec = {2: (1, 9999, "codex-cli"), 10: (2, 5000, "node a-mcp.js"), 11: (2, 6000, "node a-mcp.js")}
        table = {pid: Row(pid, ppid, age, cmd, 501) for pid, (ppid, age, cmd) in spec.items()}
        seen = []

        def reader(uid=None):
            seen.append(uid)
            return table

        plan = build_plan(table, uid=501, self_pid=999)
        result = execute(plan, {t.pid: "same" for t in plan.reap}, uid=501,
                         read_table=reader, identity=lambda pid: "same", terminate=lambda pid: None)
        self.assertIn(501, seen, "revalidation must read the same uid the plan was built from")
        self.assertTrue(result.clean, result.skipped)


if __name__ == "__main__":
    unittest.main(verbosity=2)
