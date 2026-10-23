"""Third-party kernel sources (config/kernel-contrib.json) stay pinned and opt-in."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/kernel-contrib.json"
SCRIPT = ROOT / "scripts/kernel-contrib.sh"
PATCH = ROOT / "patches/0010-crypto-ipsecmb.patch"


def run_contrib(src, **env):
    environment = {key: value for key, value in os.environ.items() if key != "MODULES_OVERRIDE"}
    environment.update(env)
    return subprocess.run(["sh", str(SCRIPT), str(src)], env=environment,
                          capture_output=True, text=True, check=False)


class KernelContribConfigTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads(CONFIG.read_text(encoding="utf-8"))

    def test_the_library_is_pinned_by_commit(self):
        lib = self.config["intel-ipsec-mb"]
        self.assertRegex(lib["commit"], r"^[0-9a-f]{40}$")
        self.assertTrue(lib["url"].startswith("https://github.com/intel/"))
        self.assertEqual(lib["destination"], "sys/contrib/intel-ipsec-mb")

    def test_nasm_is_pinned_by_sha256_on_https_sites(self):
        nasm = self.config["nasm"]
        self.assertRegex(nasm["sha256"], r"^[0-9a-f]{64}$")
        for site in (nasm["url"], nasm["mirror"]):
            self.assertTrue(site.startswith("https://"), site)
            self.assertTrue(site.endswith(f"nasm-{nasm['version']}.tar.xz"), site)
        # The module Makefile looks for the built assembler next to the library.
        self.assertEqual(nasm["destination"], self.config["intel-ipsec-mb"]["destination"] + "/.nasm")
        self.assertIn("../.nasm/nasm", PATCH.read_text(encoding="utf-8"))

    def test_the_shell_reader_sees_the_same_values(self):
        # kernel-contrib.sh reads the config with sed, one "key": "value" per line.
        text = CONFIG.read_text(encoding="utf-8")
        for section in ("intel-ipsec-mb", "nasm"):
            for key, value in self.config[section].items():
                self.assertEqual(len(re.findall(rf'^ *"{re.escape(key)}": *"{re.escape(value)}"', text, re.M)), 1,
                                 f"{section}.{key}")


class IpsecmbPatchTests(unittest.TestCase):
    def test_the_patch_only_adds_the_driver_and_its_module(self):
        text = PATCH.read_text(encoding="utf-8")
        files = re.findall(r"^diff --git a/(\S+) b/\1$", text, re.M)
        self.assertGreater(len(files), 0)
        self.assertEqual(text.count("\nnew file mode 100644\n") + text.startswith("new file mode"), len(files))
        for path in files:
            self.assertTrue(path.startswith(("sys/crypto/ipsecmb/", "sys/modules/ipsecmb/")), path)
        self.assertNotIn("\r", text)

    def test_every_patched_source_change_is_in_the_platform_recipe(self):
        for planner in ("scripts/plan.py", "scripts/stable_plan.py"):
            source = (ROOT / planner).read_text(encoding="utf-8")
            self.assertIn('ROOT / "scripts/kernel-contrib.sh"', source, planner)
            self.assertIn('ROOT / "config/kernel-contrib.json"', source, planner)

    def test_apply_runs_the_contrib_step_after_the_patches(self):
        apply = (ROOT / "apply.sh").read_text(encoding="utf-8")
        self.assertLess(apply.index("patches applied"), apply.index("scripts/kernel-contrib.sh"))


@unittest.skipUnless(shutil.which("sh"), "needs a POSIX sh")
class KernelContribScriptTests(unittest.TestCase):
    def setUp(self):
        self.src = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.src, ignore_errors=True)

    def test_kernels_without_ipsecmb_fetch_nothing(self):
        result = run_contrib(self.src, MODULES_OVERRIDE="ossl qat qat_c2xxx if_wg")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("skipped (ipsecmb is not in MODULES_OVERRIDE)", result.stdout)
        self.assertEqual(list(self.src.iterdir()), [])

    def test_the_skip_switch_fetches_nothing(self):
        result = run_contrib(self.src, KERNEL_CONTRIB_SKIP="1", MODULES_OVERRIDE="ipsecmb")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(self.src.iterdir()), [])

    def test_a_nasm_tarball_with_the_wrong_hash_fails(self):
        library = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, library, ignore_errors=True)
        (library / "lib").mkdir()
        (library / "lib/intel-ipsec-mb.h").write_text("/* stand-in */\n", encoding="utf-8")
        tarball = library / "nasm.tar.xz"
        tarball.write_bytes(b"not the pinned nasm")
        result = run_contrib(self.src, MODULES_OVERRIDE="ossl ipsecmb",
                             IPSECMB_SRC=str(library), NASM_TARBALL=str(tarball))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("nasm tarball sha256", result.stderr)
        self.assertTrue((self.src / "sys/contrib/intel-ipsec-mb/lib/intel-ipsec-mb.h").exists())
        self.assertFalse((self.src / "sys/contrib/intel-ipsec-mb/.nasm/nasm").exists())

    def test_a_library_copy_that_is_not_the_library_fails(self):
        library = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, library, ignore_errors=True)
        result = run_contrib(self.src, MODULES_OVERRIDE="ipsecmb", IPSECMB_SRC=str(library))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is not an intel-ipsec-mb tree", result.stderr)


if __name__ == "__main__":
    unittest.main()
