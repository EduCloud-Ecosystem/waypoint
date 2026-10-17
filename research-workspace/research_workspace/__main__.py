"""Operator commands. Credentials are printed only when deliberately created."""

import argparse
import json
from pathlib import Path
import sys

from .runtime import Broker, validate_images
from .store import Store, canonical, private_write


def main():
    parser = argparse.ArgumentParser(description="EduCloud local research workspace")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create fresh private local authority")
    init.add_argument("--state", required=True)
    init.add_argument("--broker", required=True)
    init.add_argument("--subject", default="researcher")
    init.add_argument("--python-image")
    init.add_argument("--r-image")
    init.add_argument("--total-budget-cpu-seconds", type=int, default=600)
    for name in ("user-add", "user-revoke"):
        command = commands.add_parser(name)
        command.add_argument("--state", required=True)
        command.add_argument("--subject", required=True)
    serve_parser = commands.add_parser("serve")
    serve_parser.add_argument("--state", required=True)
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=18765)
    verify = commands.add_parser("verify-bundle")
    verify.add_argument("bundle")
    args = parser.parse_args()
    if args.command == "init":
        images = {key: value for key, value in {"python": args.python_image, "r": args.r_image}.items() if value}
        validate_images(images)
        if not 30 <= args.total_budget_cpu_seconds <= 36000:
            raise ValueError("Choose a finite total allowance between 30 and 36,000 CPU-seconds.")
        broker = Broker(args.broker, Path(args.state) / "broker")
        store = Store(args.state, create=True)
        private_write(store.root / "config.json", canonical({"version": 1, "mode": "local-public-data", "broker": broker.executable,
                      "images": images, "total_budget_cpu_seconds": args.total_budget_cpu_seconds}).encode())
        key = store.add_user(args.subject)
        print(json.dumps({"subject": args.subject, "access_key": key, "note": "Save this private access key. It is displayed only once."}))
    elif args.command == "user-add":
        key = Store(args.state).add_user(args.subject)
        print(json.dumps({"subject": args.subject, "access_key": key}))
    elif args.command == "user-revoke":
        Store(args.state).revoke_user(args.subject)
        print('{"revoked":true}')
    elif args.command == "serve":
        from .server import serve
        serve(args.state, args.host, args.port)
    elif args.command == "verify-bundle":
        from .evidence import verify_bundle
        path = Path(args.bundle)
        if path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("Bundle exceeds the pilot's verification size bound.")
        print(json.dumps(verify_bundle(path.read_bytes()), indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
