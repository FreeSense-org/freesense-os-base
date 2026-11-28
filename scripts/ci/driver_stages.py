#!/usr/bin/env python3
"""Tell the build workflow which stages build/build has, so it can split them into jobs.

usage: driver_stages.py build/build

Prints `base_stage=` (where the first long job stops), `repo_stage=` (where the unsigned package repository is
finished) and `complete=` (whether the driver makes a repository to sign and an installer image from it).
"""
# Copyright (c) 2026 The FreeSense Project. SPDX-License-Identifier: Apache-2.0

from pathlib import Path
import runpy
import sys


def plan(stages: tuple) -> dict[str, str]:
    base = next((name for name in ("base", "source", "check") if name in stages), None)
    if base is None:
        raise ValueError("build/build has none of the stages check, source and base")
    repo = "repo" if "repo" in stages else ""
    return {"base_stage": base, "repo_stage": repo, "complete": "true" if repo and "iso" in stages else "false"}


def stages_of(driver: Path) -> tuple:
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(driver.parent))
    return tuple(runpy.run_path(str(driver), run_name="freesense_build")["STAGES"])


if __name__ == "__main__":
    for key, value in plan(stages_of(Path(sys.argv[1]).resolve())).items():
        print(f"{key}={value}")
