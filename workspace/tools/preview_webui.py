"""Serve the real FreeSense WebUI on this computer for a look, without building an image.

Usage: python tools/preview_webui.py [--port 8080]

The shell, assets and management router are the working tree's own. The router runs in the PHP 8.6 container on the
synthetic pinned-format configuration used by the sign-in browser test (user `admin`, password `fixture-admin-pass`).
There is no FreeBSD behind it: pages that read the appliance (interfaces, firewall state, services) show what the
router can say without it, and changes that need the operating system are refused. Stop it with Ctrl-C.
"""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0
import argparse
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Event

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "freesense/tests/web"))
sys.dont_write_bytecode = True
import test_sign_in as harness_module  # noqa: E402  (the harness and the HTTP front end live with the sign-in test)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    harness = harness_module.Harness(harness_module.STAGE.parent)
    harness.ask({"control": "reset"})
    harness.ask({"control": "settings", "values": {"threshold": 30, "block": 120, "window": 1800, "pass": []}})
    server = ThreadingHTTPServer(("127.0.0.1", args.port), harness_module.Appliance)
    server.harness, server.log = harness, []
    print(f"FreeSense WebUI preview: http://127.0.0.1:{args.port}/   sign in as admin / fixture-admin-pass", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        harness.close()


if __name__ == "__main__":
    main()
