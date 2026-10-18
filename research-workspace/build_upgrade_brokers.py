"""Build only the two pinned standard-library brokers; no downloads or providers."""
import argparse
import json
import os
from pathlib import Path
import subprocess

from research_workspace.store import canonical, private_write
from verify_broker_upgrade import PINS, digest, file_hash, new_private_directory, observed_command, strict_json


def build(repository, output):
    pins = strict_json(PINS.read_bytes())
    # Validate both Git identities before creating the output directory.
    for pin in pins.values():
        tree = subprocess.check_output(["git", "-C", str(repository), "rev-parse", pin["commit"] + "^{tree}"], timeout=10).decode().strip()
        if tree != pin["tree"]:
            raise ValueError("Pinned source tree mismatch.")
    root = new_private_directory(output)
    environment = {**os.environ, "GOPROXY": "off", "GOSUMDB": "off", "GOTOOLCHAIN": "local", "GOFLAGS": "", "CGO_ENABLED": "0", "GOWORK": "off"}
    for role, pin in pins.items():
        source = root / (role + "-source")
        source.mkdir(mode=0o700)
        for name, expected in pin["source_sha256"].items():
            content = subprocess.check_output(["git", "-C", str(repository), "show", pin["commit"] + ":" + name], timeout=10)
            if digest(content) != expected:
                raise ValueError("Pinned source content mismatch.")
            target = source / name
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            private_write(target, content)
        binary = root / role
        command = ["go", "build", "-buildvcs=false", "-o", str(binary), "./cmd/outfitter"]
        process = subprocess.Popen(command, cwd=source, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        from research_workspace.bounded_process import collect
        stdout, stderr = collect(process, timeout=120)
        private_write(root / (role + ".build.stdout"), stdout)
        private_write(root / (role + ".build.stderr"), stderr)
        if process.returncode:
            raise ValueError("Pinned broker build failed; retained private logs describe the failure.")
        code, metadata, _ = observed_command(["go", "version", "-m", str(binary)])
        if code:
            raise ValueError("Go build metadata unavailable.")
        private_write(root / (role + ".provenance.json"), canonical({
            "format": "waypoint-broker-build-v1", "role": role, "pin": pin,
            "binary_sha256": file_hash(binary), "build_metadata": metadata.decode().splitlines()[1:],
            "build_command": command, "environment_overrides": {k: environment[k] for k in ("GOPROXY", "GOSUMDB", "GOTOOLCHAIN", "GOFLAGS", "CGO_ENABLED", "GOWORK")},
            "pins_sha256": file_hash(PINS), "builder_sha256": file_hash(__file__),
        }).encode())
    return {role: str(root / role) for role in pins}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.repository, args.output)))
