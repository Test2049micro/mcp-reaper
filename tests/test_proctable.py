import os
import unittest

from mcp_reaper.proctable import Row, ancestry, etime_to_sec, parse_ps


class EtimeTest(unittest.TestCase):
    def test_mmss(self):
        self.assertEqual(etime_to_sec("01:30"), 90)

    def test_hhmmss(self):
        self.assertEqual(etime_to_sec("02:03:04"), 7384)

    def test_days(self):
        self.assertEqual(etime_to_sec("1-02:03:04"), 93784)


class ParsePsTest(unittest.TestCase):
    # uids are passed explicitly everywhere: a hard-coded 501 in a fixture that
    # relies on the *default* filter would pass on this machine and fail on a CI
    # runner whose first interactive uid is different
    @classmethod
    def setUpClass(cls):
        cls.mine = os.getuid()
        cls.other = 0 if cls.mine != 0 else 65534
        cls.SAMPLE = "\n".join([
            "  UID   PID  PPID     ELAPSED COMMAND",
            f"  {cls.mine:>3}  1001     1    03:00:00 /Applications/Codex CLI.app/Contents/MacOS/codex-cli",
            f"  {cls.mine:>3}  1002  1001    01:00:00 node /opt/context7-mcp/dist/index.js",
            f"  {cls.mine:>3}  1003  1001    00:30:00 node /opt/context7-mcp/dist/index.js",
            f"  {cls.other:>3}     7     0    10:00:00 /sbin/launchd",
        ])

    def test_default_filter_keeps_only_the_current_uid(self):
        self.assertEqual(set(parse_ps(self.SAMPLE)), {1001, 1002, 1003})

    def test_explicit_uid_overrides_the_default(self):
        self.assertEqual(set(parse_ps(self.SAMPLE, uid=self.other)), {7})

    def test_row_count_and_uid_filter(self):
        table = parse_ps(self.SAMPLE, uid=self.mine)
        self.assertEqual(set(table), {1001, 1002, 1003})
        self.assertNotIn(7, table)

    def test_fields_parsed(self):
        row = parse_ps(self.SAMPLE, uid=self.mine)[1002]
        self.assertEqual((row.ppid, row.age_sec), (1001, 3600))
        self.assertIn("context7-mcp", row.command)

    def test_header_line_is_not_a_process(self):
        # the header row must not become a phantom entry
        self.assertEqual(parse_ps("  UID   PID  PPID     ELAPSED COMMAND\n"), {})


class AncestryTest(unittest.TestCase):
    def table(self):
        return {
            1: Row(1, 0, 10, "launchd", 501),
            2: Row(2, 1, 10, "Terminal", 501),
            3: Row(3, 2, 10, "codex-cli", 501),
            4: Row(4, 3, 10, "node mcp", 501),
        }

    def test_chain_nearest_first(self):
        self.assertEqual(ancestry(4, self.table()), [4, 3, 2, 1])

    def test_broken_parent_stops(self):
        table = {9: Row(9, 99, 1, "x", 501)}
        self.assertEqual(ancestry(9, table), [9])

    def test_self_parenting_loop_stops(self):
        table = {5: Row(5, 5, 1, "loop", 501)}
        self.assertEqual(ancestry(5, table), [5])


if __name__ == "__main__":
    unittest.main(verbosity=2)
