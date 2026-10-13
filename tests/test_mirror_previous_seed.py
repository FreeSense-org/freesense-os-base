"""Execute the real mirror-plus-previous-repository seed composition."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def shell() -> str | None:
    found = shutil.which("bash") or shutil.which("sh")
    if found is None and os.name == "nt":
        candidate = Path("C:/Program Files/Git/bin/bash.exe")
        found = str(candidate) if candidate.exists() else None
    return found


def function(text: str, name: str) -> str:
    start = text.index(f"{name}() ")
    # a function body closes with a lone brace, or a lone parenthesis when it
    # runs in a subshell
    end = min(index for index in (text.find("\n}\n", start), text.find("\n)\n", start)) if index != -1)
    return text[start:end + 3]


class MirrorPreviousSeedTests(unittest.TestCase):
    def setUp(self) -> None:
        if shell() is None or shutil.which("jq") is None:
            self.skipTest("POSIX shell or jq unavailable")

    def compose(self, system_names=()):
        common = (ROOT / "scripts/runner/worker-common.sh").read_text(encoding="utf-8")
        body = (function(common, "merge_package") + function(common, "merge_previous_seed")
                + function(common, "prepare_mirror_seed"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Keep the function bodies verbatim; only the absolute roots move.
            body = body.replace("/root/", "@ROOT@/root/").replace("/tmp/", "@ROOT@/tmp/").replace("@ROOT@", root.as_posix())
            for part in ("root/mirror-repo/All", "root/previous-freesense-seed/All", "root/system/All", "tmp"):
                (root / part).mkdir(parents=True)

            def package(where: str, name: str, version: str = "1.0") -> None:
                (root / where / f"{name}-{version}.pkg").write_text(f"{name}|{version}|net/{name}||\nbytes\n")

            package("root/mirror-repo/All", "mirrored")
            package("root/mirror-repo/All", "overlap")
            package("root/previous-freesense-seed/All", "unchanged")
            package("root/previous-freesense-seed/All", "overlap", "9.9")
            package("root/previous-freesense-seed/All", "retired")
            package("root/previous-freesense-seed/All", "from-system")
            for name in system_names:
                package("root/system/All", name)
            (root / "root/mirror-plan.json").write_text(json.dumps({
                "packages": [{"name": "mirrored"}, {"name": "overlap"}],
                "delta_packages": ["unchanged", "from-system"],
            }))
            script = f"""set -eu
package_metadata() {{ head -n 1 "$1"; }}
sha256() {{ sha256sum "$2" | cut -d ' ' -f 1; }}
pkg() {{ head -n 1 "$3" | cut -d '|' -f 1; }}
load_previous_freesense_seed_if_usable() {{ :; }}
{body}
inventory=caller-inventory destination=caller-destination package=caller-package duplicate_policy=caller-policy
prepare_mirror_seed {root.as_posix()}/root/seed {root.as_posix()}/root/system
echo "caller:$inventory:$destination:$package:$duplicate_policy"
"""
            result = subprocess.run([shell(), "-c", script], cwd=root, capture_output=True, text=True)
            seeded = {file.name for file in (root / "root/seed/All").iterdir()} if (root / "root/seed/All").exists() else set()
            return result, seeded

    def test_only_unchanged_sanctioned_unsupplied_packages_are_reused(self) -> None:
        result, seeded = self.compose(system_names=("from-system",))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(seeded, {"mirrored-1.0.pkg", "overlap-1.0.pkg", "unchanged-1.0.pkg"})
        self.assertIn("1 unchanged package(s) reused", result.stdout)
        # merge_package assigns these globals; the stage scripts keep their own
        # inventory in one, so seeding must not leak into the caller's shell
        self.assertIn("caller:caller-inventory:caller-destination:caller-package:caller-policy", result.stdout)
        self.assertIn("Previous package no longer in the sealed plan, not reused: retired", result.stdout)
        self.assertIn("Seed already supplies overlap; previous copy not reused", result.stdout)
        self.assertIn("Package supplied elsewhere, previous copy not reused: from-system", result.stdout)

    def test_without_an_authoritative_repository_the_plan_still_filters(self) -> None:
        result, seeded = self.compose()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(seeded, {"mirrored-1.0.pkg", "overlap-1.0.pkg", "unchanged-1.0.pkg", "from-system-1.0.pkg"})


class WiringTests(unittest.TestCase):
    def read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_both_stages_seed_the_mirror_with_the_previous_repository(self) -> None:
        system = self.read("scripts/runner/stages/system.sh")
        self.assertIn("prepare_mirror_seed\n      seed_poudriere_repository /root/mirror-previous-seed", system)
        packages = self.read("scripts/runner/stages/packages.sh")
        self.assertIn("prepare_mirror_seed /root/mirror-previous-seed /root/system-repo", packages)
        self.assertIn("lower=/root/mirror-previous-seed", packages)

    def test_current_provenance_describes_the_previous_packages_not_an_empty_directory(self) -> None:
        common = self.read("scripts/runner/worker-common.sh")
        load = function(common, "load_previous_freesense_seed")
        self.assertIn('ln -s "${previous_package}" /tmp/current-package-provenance-repository/All/', load)
        self.assertLess(load.index("ln -s"), load.index("record_package_provenance /tmp/current-package-provenance-repository"))

    def test_an_unusable_previous_repository_falls_back_to_a_full_build(self) -> None:
        common = self.read("scripts/runner/worker-common.sh")
        prepare = function(common, "prepare_mirror_seed")
        self.assertIn("load_previous_freesense_seed_if_usable", prepare)
        usable = function(common, "load_previous_freesense_seed_if_usable")
        self.assertIn("load_previous_freesense_seed || {", usable)
        self.assertIn("building without it", usable)

    def test_the_finalize_build_fills_the_gaps_from_the_previous_repository(self) -> None:
        system = self.read("scripts/runner/stages/system.sh")
        finalize = system[system.index("  finalize)"):system.index("  full)")]
        merged = finalize.index("phase system-mirror-merged")
        self.assertLess(merged, finalize.index("load_previous_freesense_seed_if_usable"))
        self.assertLess(finalize.index("merge_previous_seed"), finalize.index("seed_poudriere_repository"))
        self.assertIn('merge_previous_seed "${shard_seed}" "${shard_inventory}"', finalize)


if __name__ == "__main__":
    unittest.main()
