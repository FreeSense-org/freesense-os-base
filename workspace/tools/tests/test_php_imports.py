"""The missing-import gate (tools/php-imports.php) reports unimported FreeSense classes and accepts imported ones."""
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 The FreeSense Project.

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

TOOL = Path(__file__).resolve().parents[1] / "php-imports.php"
HEADER = "<?php\ndeclare(strict_types=1);\n"


class MissingImports(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        lib = self.root / "component/src/usr/local/FreeSense/lib"
        self.put(lib / "System/Output.php", "namespace FreeSense\\System;\nfinal class Output {}\n")
        self.put(lib / "System/Runner.php", "namespace FreeSense\\System;\ninterface Runner {}\ntrait Logs {}\n")
        self.user = lib / "App/User.php"
        shutil.copyfile(TOOL, self.root / "php-imports.php")

    def put(self, path: Path, code: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((HEADER + code).encode())

    def run_gate(self):
        return subprocess.run(["docker", "run", "--rm", "--pull=never", "--network", "none", "-v", f"{self.root}:/check:ro",
                               "php:8.6.0-cli", "php", "/check/php-imports.php", "/check"], capture_output=True, text=True)

    def test_imported_names_pass(self):
        self.put(self.user, "namespace FreeSense\\App;\nuse FreeSense\\System\\{Output,Runner as Runs,Logs};\nuse RuntimeException;\n"
                 "final class User implements Runs {\n\tuse Logs;\n\tpublic function a(Output|null $o, ?self $s = null): Output {\n"
                 "\t\tif (!$o instanceof Output) throw new RuntimeException(Output::class);\n\t\treturn new Output();\n\t}\n"
                 "\tpublic function b(): string { return class_exists(\\FreeSense\\Later\\Thing::class) ? \\FreeSense\\Later\\Thing::now() : ''; }\n}\n")
        result = self.run_gate()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_every_kind_of_reference_is_reported(self):
        self.put(self.user, "namespace FreeSense\\App;\nfinal class User extends Base implements Contract {\n\tuse Shared;\n"
                 "\tprivate ?Property $p = null;\n\tpublic function a(Argument &...$x): Returned {\n"
                 "\t\ttry { Statically::read(); new Created(); } catch (Caught $e) {}\n\t\treturn $x[0] instanceof Compared;\n\t}\n}\n")
        result = self.run_gate()
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        for name in ["Base", "Contract", "Shared", "Property", "Argument", "Returned", "Statically", "Created", "Caught", "Compared"]:
            self.assertIn(f"declares FreeSense\\App\\{name} (", result.stderr)
