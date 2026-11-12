import unittest
from run_note_partition import select_range, validate_partition


class PartitionTests(unittest.TestCase):
    def test_ranges_are_disjoint_and_preserve_source_identity(self):
        rows = [{"case_id": str(i)} for i in range(11)]
        a = list(select_range(iter(rows), 4, 7))
        b = list(select_range(iter(rows), 7, 11))
        self.assertEqual(a + b, rows[4:])
        self.assertFalse({r["case_id"] for r in a} & {r["case_id"] for r in b})

    def test_invalid_partition_fails_before_inference(self):
        for start, stop in ((-1, 4), (4, 4), (5, 4), (0, 353654)):
            with self.assertRaises(ValueError):
                validate_partition(start, stop)
