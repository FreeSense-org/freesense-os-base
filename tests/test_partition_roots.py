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
        shards = partition_roots.partition(roots, ["sysutils/telegraf"], 8)
        self.assertEqual(shards[0], ["sysutils/telegraf"])
        self.assertEqual(sorted(root for shard in shards for root in shard), sorted(set(roots)))
        self.assertEqual(shards, partition_roots.partition(list(reversed(roots)), ["sysutils/telegraf"], 8))

    def test_absent_heavy_root_does_not_reserve_an_empty_shard(self):
        shards = partition_roots.partition([f"devel/p{i}" for i in range(8)], ["sysutils/telegraf"], 8)
        self.assertTrue(all(shards))
        self.assertEqual([len(shard) for shard in shards], [1] * 8)

    def test_small_root_set_exposes_empty_shards_deterministically(self):
        shards = partition_roots.partition(["devel/a", "devel/b"], [], 8)
        self.assertEqual(shards, [["devel/a"], ["devel/b"], [], [], [], [], [], []])

    def test_policy_rejects_invalid_shape_and_origins(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "policy.json")
            path.write_text(json.dumps({"schema_version": "wrong", "count": 4,
                                        "measured_heavy_roots": {"system": [], "packages": []}}))
            with self.assertRaisesRegex(ValueError, "policy"):
                partition_roots.load(path, "system", ["devel/a"])
        for roots, heavy, count in ((["../bad/root"], [], 8), (["devel/a"], [], 0),
                                    ([f"devel/{i}" for i in range(8)], [f"devel/{i}" for i in range(8)], 8)):
            with self.subTest(roots=roots), self.assertRaises(ValueError):
                partition_roots.partition(roots, heavy, count)

    def test_dependency_closures_stay_together_and_batches_are_cumulative(self):
        roots = ["devel/a", "devel/b", "devel/c"]
        shards = partition_roots.partition(roots, [], 8,
            closures={"devel/a": ["lang/rust"], "devel/b": ["lang/rust"], "devel/c": ["lang/go"]},
            costs={"devel/a": 4000, "devel/b": 3000, "devel/c": 2000})
        self.assertTrue(any(set(shard) == {"devel/a", "devel/b"} for shard in shards))
        self.assertEqual(partition_roots.batches(roots, {root: 5000 for root in roots}),
                         [["devel/a", "devel/b"], roots])
        self.assertEqual(partition_roots.batches(["devel/a"], {"devel/a": 10801}), [["devel/a"]])


class ProductNameSubstitutionTests(unittest.TestCase):
    """The Optional root list is a template; partitioning it raw never worked."""

    def test_a_templated_origin_is_not_a_partitionable_root(self):
        # poudriere_packages spells 34 of its origins %%PRODUCT_NAME%%-pkg-*.
        # ORIGIN does not admit '%', so the whole plan is rejected -- not the
        # one bad entry -- before any package is built.
        with self.assertRaisesRegex(ValueError, "invalid shard root plan"):
            partition_roots.partition(
                ["dns/%%PRODUCT_NAME%%-pkg-bind", "dns/dnsmasq"], [], 8)
        self.assertEqual(
            [["dns/FreeSense-pkg-bind", "dns/dnsmasq"]][0],
            sorted(sum(partition_roots.partition(
                ["dns/FreeSense-pkg-bind", "dns/dnsmasq"], [], 8), [])))

    def test_no_farm_stage_turns_the_template_into_roots_unsubstituted(self):
        """The root list a stage partitions must never hold %%PRODUCT_NAME%%.

        Asserted against the pipelines themselves rather than their position in
        the file: the partition call is shared between the plan path and the
        legacy path, so it does not sit after the substitution in source order
        and an ordering test would only measure where the function happens to
        be defined.
        """
        root = Path(__file__).resolve().parents[1] / "scripts/runner/stages"
        template = "tools/conf/pfPorts/poudriere_bulk"
        substitution = "s/%%PRODUCT_NAME%%/FreeSense/g"
        checked = 0
        for stage in ("system.sh", "packages.sh"):
            text = (root / stage).read_text(encoding="utf-8")
            self.assertIn("scripts/partition_roots.py", text)
            # Join backslash continuations so a pipeline is one logical line.
            logical = text.replace(chr(92) + chr(10), " ").splitlines()
            for line in logical:
                # A pipeline that reads the template and writes a roots file.
                if template not in line:
                    continue
                if not any(sink in line for sink in
                           ('>"${all_roots}"', ">/tmp/optional-all-roots")):
                    continue
                checked += 1
                with self.subTest(stage=stage, line=line.strip()[:70]):
                    self.assertIn(substitution, line,
                                  f"{stage} builds roots from the template without substituting")
        self.assertEqual(checked, 2, "expected one roots pipeline per farm stage")
