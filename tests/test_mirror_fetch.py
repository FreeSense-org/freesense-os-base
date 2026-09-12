import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import mirror_fetch
from multiarch_pin import digest

ABI = "FreeBSD:16:amd64"


def plan(*packages, architecture="amd64"):
    document = {
        "schema_version": "freesense.mirror-plan/v1", "abi": ABI, "architecture": architecture,
        "ports_commit": "a" * 40, "catalog_sha256": "b" * 64, "delta_sha256": "c" * 64,
        "packages": list(packages), "delta_roots": [], "collisions": [],
        "counts": {"mirror": len(packages), "delta": 0, "churn": 0},
    }
    document["fingerprint"] = digest(document)
    return document


def package(name, payload=b"package-bytes", *, version="1.0"):
    return {
        "name": name, "version": version, "origin": f"devel/{name}", "abi": ABI,
        "file": f"All/{name}-{version}.pkg", "upstream_path": f"All/{name}-{version}.pkg",
        "upstream_checksum": hashlib.sha256(payload).hexdigest(),
        "size": len(payload), "catalog_sha256": "b" * 64,
    }


class UrlTests(unittest.TestCase):
    def test_the_url_is_built_from_the_abi_and_the_catalogue_path(self):
        self.assertEqual(mirror_fetch.package_url(ABI, "All/curl-8.22.0.pkg"),
                         "https://pkg.freebsd.org/FreeBSD:16:amd64/latest/All/curl-8.22.0.pkg")

    def test_plain_http_and_unknown_abi_are_refused(self):
        with self.assertRaisesRegex(ValueError, "https"):
            mirror_fetch.package_url(ABI, "All/a-1.pkg", base_url="http://pkg.freebsd.org")
        with self.assertRaisesRegex(ValueError, "ABI"):
            mirror_fetch.package_url("FreeBSD:16:riscv", "All/a-1.pkg")

    def test_a_path_escaping_the_repository_is_refused(self):
        for path in ("../../etc/passwd", "All/../../x.pkg", "Latest/pkg.pkg", "All/x.txt"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                mirror_fetch.package_url(ABI, path)


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.requested = []

    def fetcher(self, payloads):
        def fetch_one(url, destination):
            self.requested.append(url)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payloads[destination.name])
        return fetch_one

    def test_every_package_is_downloaded_and_recorded(self):
        payload = b"package-bytes"
        document = plan(package("alpha", payload), package("beta", payload))
        provenance = mirror_fetch.fetch(
            document, self.directory,
            fetch_one=self.fetcher({"alpha-1.0.pkg": payload, "beta-1.0.pkg": payload}))
        self.assertEqual(len(provenance["packages"]), 2)
        self.assertEqual(provenance["schema_version"], "freesense.mirror-provenance/v1")
        self.assertEqual(provenance["plan_fingerprint"], document["fingerprint"])
        self.assertEqual(provenance["packages"][0]["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertTrue((self.directory / "All" / "alpha-1.0.pkg").is_file())
        self.assertEqual(self.requested, [
            "https://pkg.freebsd.org/FreeBSD:16:amd64/latest/All/alpha-1.0.pkg",
            "https://pkg.freebsd.org/FreeBSD:16:amd64/latest/All/beta-1.0.pkg"])

    def test_a_package_that_changed_upstream_fails_the_mirror(self):
        # Same length as the planned payload, so this exercises the checksum
        # rather than the cheaper size check.
        document = plan(package("alpha", b"package-bytes"))
        self.assertEqual(len(b"tampered-data"), len(b"package-bytes"))
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            mirror_fetch.fetch(document, self.directory,
                               fetch_one=self.fetcher({"alpha-1.0.pkg": b"tampered-data"}))

    def test_a_truncated_transfer_fails_the_mirror(self):
        entry = package("alpha", b"package-bytes")
        entry["size"] = 999
        with self.assertRaisesRegex(ValueError, "size mismatch"):
            mirror_fetch.fetch(plan(entry), self.directory,
                               fetch_one=self.fetcher({"alpha-1.0.pkg": b"package-bytes"}))

    def test_a_tampered_plan_is_refused_before_anything_is_downloaded(self):
        document = plan(package("alpha"))
        document["packages"][0]["upstream_checksum"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            mirror_fetch.fetch(document, self.directory, fetch_one=self.fetcher({}))
        self.assertEqual(self.requested, [])

    def test_a_stray_package_in_the_directory_is_refused(self):
        payload = b"package-bytes"
        (self.directory / "All").mkdir(parents=True)
        (self.directory / "All" / "unplanned-9.9.pkg").write_bytes(payload)
        with self.assertRaisesRegex(ValueError, "the plan does not name"):
            mirror_fetch.fetch(plan(package("alpha", payload)), self.directory,
                               fetch_one=self.fetcher({"alpha-1.0.pkg": payload}))

    def test_an_empty_plan_is_refused(self):
        with self.assertRaisesRegex(ValueError, "no packages"):
            mirror_fetch.fetch(plan(), self.directory, fetch_one=self.fetcher({}))

    def test_a_plan_of_the_wrong_schema_is_refused(self):
        document = plan(package("alpha"))
        document["schema_version"] = "freesense.binary-seed/v1"
        with self.assertRaisesRegex(ValueError, "invalid mirror plan"):
            mirror_fetch.fetch(document, self.directory, fetch_one=self.fetcher({}))


if __name__ == "__main__":
    unittest.main()
