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
    SAMPLE = "\n".join([
        "  UID   PID  PPID     ELAPSED COMMAND",
        "  501  1001     1    03:00:00 /Applications/Codex CLI.app/Contents/MacOS/codex-cli",
        "  501  1002  1001    01:00:00 node /opt/context7-mcp/dist/index.js",
        "  501  1003  1001    00:30:00 node /opt/context7-mcp/dist/index.js",
        "    0     7     0    10:00:00 /sbin/launchd",
    ])

    def test_row_count_and_uid_filter(self):
        table = parse_ps(self.SAMPLE, uid=501)
        self.assertEqual(set(table), {1001, 1002, 1003})
        self.assertNotIn(7, table)

    def test_fields_parsed(self):
        row = parse_ps(self.SAMPLE, uid=501)[1002]
        self.assertEqual((row.ppid, row.age_sec), (1001, 3600))
        self.assertIn("context7-mcp", row.command)

    def test_header_line_is_not_a_process(self):
        self.assertNotIn(0, parse_ps(self.SAMPLE, uid=None))


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
