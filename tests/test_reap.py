"""Grouping and reaping decisions.

These tests encode the two incidents this tool exists to avoid:
1. killing the newest server instead of the leaked older ones (an inverted
   ordering that looks like it works until a live session loses its server);
2. killing a server that belongs to a different, still-running session because
   no session host could be identified.
"""

import unittest

from mcp_reaper.proctable import Row
from mcp_reaper.reap import build_plan, execute


def table_of(spec):
    """spec: {pid: (ppid, age_sec, command)}"""
    return {pid: Row(pid, ppid, age, cmd, 501) for pid, (ppid, age, cmd) in spec.items()}


CLIENT = "codex-cli --session"


def session_tree(count, base_age=1000, pid_base=100, root=2, cmd="node /opt/context7-mcp/dist/index.js"):
    """pid_base holds the OLDEST copy, so higher pid means younger.

    Age is the only signal the reaper has for "leak versus live", so the fixture
    must not let pid order and age order disagree by accident.
    """
    spec = {root: (1, 9999, CLIENT), pid_base - 1: (1, 9999, "Terminal")}
    for index in range(count):
        spec[pid_base + index] = (root, base_age + 100 * (count - 1 - index), cmd)
    return spec


class OrderingTest(unittest.TestCase):
    def test_keeps_newest_and_reaps_older_duplicates(self):
        plan = build_plan(table_of(session_tree(3)), self_pid=1)
        self.assertEqual(sorted(t.pid for t in plan.reap), [100, 101])

    def test_single_server_is_never_touched(self):
        plan = build_plan(table_of(session_tree(1)), self_pid=1)
        self.assertEqual(plan.reap, [])

    def test_keep_two_leaves_two(self):
        plan = build_plan(table_of(session_tree(4)), keep=2, self_pid=1)
        self.assertEqual(sorted(t.pid for t in plan.reap), [100, 101])

    def test_groups_do_not_mix_across_sessions(self):
        spec = session_tree(2, pid_base=100, root=2)
        spec.update(session_tree(2, pid_base=200, root=3))
        plan = build_plan(table_of(spec), self_pid=1)
        self.assertEqual(sorted(t.pid for t in plan.reap), [100, 200])


class IdentificationTest(unittest.TestCase):
    def test_processes_without_the_marker_are_never_candidates(self):
        spec = {2: (1, 500, CLIENT), 10: (2, 5000, "node /opt/analytics/dist/index.js")}
        plan = build_plan(table_of(spec), self_pid=1)
        self.assertEqual(plan.reap, [])
        self.assertEqual(plan.groups, [])

    def test_unidentifiable_session_defers_instead_of_guessing(self):
        spec = {100: (1, 4000, "node a-mcp.js"), 101: (1, 3000, "node a-mcp.js")}
        plan = build_plan(table_of(spec), self_pid=1)
        self.assertEqual(plan.reap, [])
        self.assertEqual([t.pid for t in plan.deferred_ambiguous], [100])

    def test_explicit_opt_in_reaps_ambiguous_groups(self):
        spec = {100: (1, 4000, "node a-mcp.js"), 101: (1, 3000, "node a-mcp.js")}
        plan = build_plan(table_of(spec), self_pid=1, reap_ambiguous=True)
        self.assertEqual([t.pid for t in plan.reap], [100])

    def test_young_duplicate_is_protected_by_min_age(self):
        plan = build_plan(table_of(session_tree(2, base_age=10, pid_base=100)), self_pid=1)
        self.assertEqual(plan.reap, [])
        self.assertEqual([t.pid for t in plan.too_young], [100])

    def test_a_session_host_is_never_a_candidate_itself(self):
        # clients that mention "mcp" in their own argv would otherwise be reaped
        # as servers, ending the session they host
        spec = {
            2: (1, 9999, "codex-cli --mcp-bootstrap"),
            10: (2, 5000, "node /opt/context7-mcp/dist/index.js"),
            11: (2, 6000, "node /opt/context7-mcp/dist/index.js"),
        }
        plan = build_plan(table_of(spec), session_hosts=("codex",), self_pid=1)
        self.assertNotIn(2, [t.pid for t in plan.reap])
        self.assertEqual([t.pid for t in plan.reap], [11])

    def test_the_reaper_never_targets_its_own_tree(self):
        spec = {1: (0, 9999, "init"), 2: (1, 9999, CLIENT), 10: (2, 5000, "node a-mcp.js"),
                11: (2, 6000, "node a-mcp.js"), 20: (1, 7000, "node b-mcp.js"), 21: (1, 6000, "node b-mcp.js")}
        plan = build_plan(table_of(spec), self_pid=20)
        # 20 is the oldest copy in its group and would be eligible, but it is the
        # reader's own process: self-protection must survive the ordering
        self.assertNotIn(20, [t.pid for t in plan.reap])
        self.assertEqual(sorted(t.pid for t in plan.reap), [11])

    def test_rules_admit_commands_without_the_marker(self):
        from mcp_reaper.signature import Rule

        spec = {2: (1, 9999, CLIENT), 10: (2, 6000, "/opt/cua_node/bin/node ./server.mjs"),
                11: (2, 5000, "/opt/cua_node/bin/node ./server.mjs")}
        rules = [Rule("cua-server", ["cua_node"])]
        plan = build_plan(table_of(spec), rules=rules, self_pid=1)
        self.assertEqual([t.pid for t in plan.reap], [10])


class SerializationTest(unittest.TestCase):
    def test_plan_json_reports_both_protections(self):
        spec = session_tree(3, pid_base=100)
        spec[300] = (1, 5000, "node x-mcp.js")
        plan = build_plan(table_of(spec), self_pid=1)
        payload = plan.to_dict(1, 120)
        self.assertEqual(payload["eligible"], 2)
        self.assertEqual(payload["ambiguous_excluded"], 0)
        self.assertEqual(len(payload["groups"]), 2)
        self.assertTrue(all("kept" in group and "eligible" in group for group in payload["groups"]))


class ExecuteTest(unittest.TestCase):
    def setUp(self):
        self.spec = session_tree(3, pid_base=100)
        self.table = table_of(self.spec)
        self.plan = build_plan(self.table, self_pid=1)
        self.identities = {t.pid: f"501 pid{t.pid} cmd" for t in self.plan.reap}

    def _run(self, live=None, fresh=None):
        # ``self.identities`` is the plan-time snapshot; ``live`` is what a fresh
        # ps says now. The two differ only when a PID was recycled.
        live = self.identities if live is None else live
        sent = []
        result = execute(
            self.plan,
            self.identities,
            read_table=lambda uid=None: fresh if fresh is not None else self.table,
            identity=lambda pid: live.get(pid),
            terminate=lambda pid: sent.append(pid),
        )
        return result, sent

    def test_signals_every_eligible_target(self):
        result, sent = self._run()
        self.assertEqual(sorted(sent), [100, 101])
        self.assertTrue(result.clean)

    def test_recycled_pid_is_skipped(self):
        live = dict(self.identities)
        live[100] = "501 pid100 SOMETHING ELSE NOW"
        result, sent = self._run(live=live)
        self.assertEqual(sent, [101])
        self.assertIn("identity_changed", [s["reason"] for s in result.skipped])

    def test_process_that_already_exited_is_skipped(self):
        gone = {pid: row for pid, row in self.table.items() if pid != 100}
        result, sent = self._run(fresh=gone)
        self.assertEqual(sent, [101])
        self.assertIn("gone", [s["reason"] for s in result.skipped])

    def test_target_that_loses_its_session_host_is_skipped(self):
        # session host gone: the two servers can no longer be attributed to one
        # session, so "this one is the leak" is no longer a derivable claim
        orphaned = {pid: Row(pid, 1 if pid > 10 else row.ppid, row.age_sec, row.command, row.uid)
                    for pid, row in self.table.items() if pid != 2}
        result, sent = self._run(fresh=orphaned)
        self.assertEqual(sent, [])
        self.assertTrue(all(s["reason"] == "no_longer_eligible" for s in result.skipped), result.skipped)

    def test_signal_failure_is_reported_not_swallowed(self):
        def fail(pid):
            raise PermissionError(pid)

        result = execute(
            self.plan,
            self.identities,
            read_table=lambda uid=None: self.table,
            identity=lambda pid: self.identities.get(pid),
            terminate=fail,
        )
        self.assertEqual(len(result.failed), 2)
        self.assertEqual(result.signalled, [])

    def test_empty_plan_signals_nothing(self):
        plan = build_plan(table_of(session_tree(1)), self_pid=1)
        result = execute(plan, {}, read_table=lambda uid=None: {}, identity=lambda pid: None,
                         terminate=lambda pid: (_ for _ in ()).throw(AssertionError("must not fire")))
        self.assertEqual(result.signalled, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
