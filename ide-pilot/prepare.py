# SPDX-License-Identifier: Apache-2.0
"""Create an isolated persistent synthetic workspace for the local IDE pilot."""
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("output", type=Path)
args = parser.parse_args()
destination = args.output.resolve()
destination.mkdir(parents=True, exist_ok=False, mode=0o700)
project = destination / "project"
shutil.copytree(Path(__file__).parent / "project", project)
(destination / "state/User").mkdir(parents=True)
(destination / "state/User/settings.json").write_text(json.dumps({
    "workbench.startupEditor": "none",
    "editor.minimap.enabled": False,
    "editor.fontSize": 16,
    "editor.wordWrap": "on",
    "workbench.colorTheme": "Default Light Modern",
    "terminal.integrated.fontSize": 16,
    "git.openRepositoryInParentFolders": "never",
}))
descriptor = os.open(destination / "server.env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "w") as stream:
    stream.write("PASSWORD=" + secrets.token_urlsafe(32) + "\n")
for command in [
    ["git", "init", "-b", "main"],
    ["git", "config", "user.name", "Waypoint Synthetic Pilot"],
    ["git", "config", "user.email", "pilot@example.invalid"],
    ["git", "config", "core.pager", "cat"],
    ["git", "add", "."],
    ["git", "commit", "-m", "Start synthetic missing-readings exercise"],
]:
    subprocess.run(command, cwd=project, check=True, stdout=subprocess.DEVNULL)
print("Prepared persistent project and local server credentials.")
