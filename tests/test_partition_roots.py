from pathlib import Path
import json
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import partition_roots


class PartitionRootsTests(unittest.TestCase):
    def test_measured_root_is_alone_and_every_root_appears_once(self):
        roots = ["net/b", "sysutils/telegraf", "net/a", "devel/c", "devel/d", "net/a"]
        shards = partition_roots.partition(roots, ["sysutils/telegraf"], 4)
        self.assertEqual(shards[0], ["sysutils/telegraf"])
        self.assertEqual(sorted(root for shard in shards for root in shard), sorted(set(roots)))
        self.assertEqual(shards, partition_roots.partition(list(reversed(roots)), ["sysutils/telegraf"], 4))

    def test_absent_heavy_root_does_not_reserve_an_empty_shard(self):
        shards = partition_roots.partition([f"devel/p{i}" for i in range(8)], ["sysutils/telegraf"], 4)
        self.assertTrue(all(shards))
        self.assertEqual([len(shard) for shard in shards], [2, 2, 2, 2])

    def test_small_root_set_exposes_empty_shards_deterministically(self):
        shards = partition_roots.partition(["devel/a", "devel/b"], [], 4)
        self.assertEqual(shards, [["devel/a"], ["devel/b"], [], []])

    def test_policy_rejects_invalid_shape_and_origins(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "policy.json")
            path.write_text(json.dumps({"schema_version": "wrong", "count": 4,
                                        "measured_heavy_roots": {"system": [], "packages": []}}))
            with self.assertRaisesRegex(ValueError, "policy"):
                partition_roots.load(path, "system", ["devel/a"])
        for roots, heavy, count in ((["../bad/root"], [], 4), (["devel/a"], [], 3),
                                    ([f"devel/{name}" for name in "abcd"], [f"devel/{name}" for name in "abcd"], 4)):
            with self.subTest(roots=roots), self.assertRaises(ValueError):
                partition_roots.partition(roots, heavy, count)
