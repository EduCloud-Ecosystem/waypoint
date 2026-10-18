"""Operator commands. Credentials are printed only when deliberately created."""

import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

from .runtime import Broker, validate_images
from .store import Store, canonical, private_write


def read_bundle(path):
    from .evidence import MAX_ARCHIVE_BYTES
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ARCHIVE_BYTES:
            raise ValueError("Use a bounded regular evidence file.")
        with os.fdopen(descriptor, "rb") as source:
            descriptor = None
            payload = source.read(MAX_ARCHIVE_BYTES + 1)
        if len(payload) != info.st_size:
            raise ValueError("The evidence file changed while being read.")
        return payload
    finally:
        if descriptor is not None:
            os.close(descriptor)


def main():
    parser = argparse.ArgumentParser(description="EduCloud local research workspace")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create fresh private local authority")
    init.add_argument("--state", required=True)
    init.add_argument("--operation-guard", action="store_true", help="Explicit fresh disposable operation-guard profile; no quota enforcement or migration")
    profiles = init.add_mutually_exclusive_group()
    profiles.add_argument("--finite-authority", action="store_true", help="Fresh opt-in local key profile with logical row/field limits; no storage quota")
    profiles.add_argument("--bounded-export", action="store_true", help="Fresh local key profile with finite limits and scoped export readers; no storage quota")
    profiles.add_argument("--bounded-guard-publication", action="store_true", help="Fresh finite local profile with fixed guard scratch; exhaustion refuses safety actions and retains debt; no storage quota")
    init.add_argument("--broker", required=True)
    init.add_argument("--subject", default="researcher")
    init.add_argument("--python-image")
    init.add_argument("--r-image")
    init.add_argument("--total-budget-cpu-seconds", type=int, default=600)
    for name in ("user-add", "user-revoke", "user-rotate"):
        command = commands.add_parser(name)
        command.add_argument("--state", required=True)
        command.add_argument("--subject", required=True)
    serve_parser = commands.add_parser("serve")
    serve_parser.add_argument("--state", required=True)
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=18765)
    verify = commands.add_parser("verify-bundle")
    verify.add_argument("bundle")
    for name in ("plan-reproduction", "reproduce"):
        command = commands.add_parser(name)
        command.add_argument("bundle")
        command.add_argument("--trusted-sha256", required=True)
        command.add_argument("--run-id", action="append")
        if name == "reproduce":
            command.add_argument("--output", required=True)
            command.add_argument("--broker", required=True)
            command.add_argument("--budget-cpu-seconds", type=int, required=True)
    backup = commands.add_parser("checkpoint", help="Capture a private offline recovery point")
    backup.add_argument("--state", required=True)
    backup.add_argument("--output", required=True)
    backup.add_argument("--broker")
    restore = commands.add_parser("restore", help="Restore a fresh replacement in held mode")
    restore.add_argument("--checkpoint", required=True)
    restore.add_argument("--trusted-sha256", required=True)
    restore.add_argument("--state", required=True)
    restore.add_argument("--broker", required=True)
    status = commands.add_parser("recovery-status")
    status.add_argument("--state", required=True)
    for name in ("storage-status", "retention-plan"):
        command = commands.add_parser(name, help="Read-only bounded offline inventory; never deletion or admission authority")
        command.add_argument("--state", required=True)
    activate = commands.add_parser("activate-recovery", help="Fence a current offline source and activate its replacement")
    activate.add_argument("--state", required=True)
    activate.add_argument("--source-state", required=True)
    activate.add_argument("--checkpoint", required=True)
    activate.add_argument("--trusted-sha256", required=True)
    args = parser.parse_args()
    if args.command == "init":
        images = {key: value for key, value in {"python": args.python_image, "r": args.r_image}.items() if value}
        validate_images(images)
        if not 30 <= args.total_budget_cpu_seconds <= 36000:
            raise ValueError("Choose a finite total allowance between 30 and 36,000 CPU-seconds.")
        broker = Broker(args.broker, Path(args.state) / "broker")
        config = {"version": 1, "mode": "local-public-data", "broker": broker.executable,
                  "images": images, "total_budget_cpu_seconds": args.total_budget_cpu_seconds}
        if args.operation_guard or args.finite_authority or args.bounded_export or args.bounded_guard_publication:
            config["mode"] = "local-guarded-public-data-v1"
            if args.finite_authority or args.bounded_export or args.bounded_guard_publication:
                from . import finite_authority as finite
                version = 3 if args.bounded_guard_publication else 2 if args.bounded_export else 1
                config.update(mode=finite.PROFILE_MODES[version], logical_authority=finite.profile(version))
            store = Store(args.state, create=True, guarded=True, config=config, subject=args.subject)
            key = store.initial_key
        else:
            store = Store(args.state, create=True)
            private_write(store.root / "config.json", canonical(config).encode())
            key = store.add_user(args.subject)
        print(json.dumps({"subject": args.subject, "access_key": key, "note": "Save this private access key. It is displayed only once."}))
    elif args.command == "user-add":
        key = Store(args.state).add_user(args.subject)
        print(json.dumps({"subject": args.subject, "access_key": key}))
    elif args.command == "user-revoke":
        Store(args.state).revoke_user(args.subject)
        print('{"revoked":true}')
    elif args.command == "user-rotate":
        key = Store(args.state).rotate_user(args.subject)
        print(json.dumps({"subject": args.subject, "access_key": key}))
    elif args.command == "serve":
        from .server import serve
        serve(args.state, args.host, args.port)
    elif args.command == "verify-bundle":
        from .evidence import verify_bundle
        print(json.dumps(verify_bundle(read_bundle(args.bundle)), indent=2))
    elif args.command in ("plan-reproduction", "reproduce"):
        from .reproduction import plan_reproduction, reproduce_bundle
        if args.command == "plan-reproduction":
            result = plan_reproduction(read_bundle(args.bundle), args.trusted_sha256, args.run_id)
        else:
            result = reproduce_bundle(args.bundle, trusted_sha256=args.trusted_sha256, output=args.output,
                                      broker=args.broker, budget_cpu_seconds=args.budget_cpu_seconds, selected_run_ids=args.run_id)
        print(json.dumps(result, indent=2))
    elif args.command in ("storage-status", "retention-plan"):
        from .storage_inventory import observe
        result = observe(args.state, retention=args.command == "retention-plan")
        print(json.dumps(result, indent=2))
        if not result["complete"]:
            raise SystemExit(1)
    elif args.command in ("checkpoint", "restore", "recovery-status", "activate-recovery"):
        from . import recovery
        if args.command == "checkpoint":
            result = recovery.checkpoint(args.state, args.output, broker=args.broker)
        elif args.command == "restore":
            result = recovery.restore(args.checkpoint, args.state, trusted_sha256=args.trusted_sha256, broker=args.broker)
        elif args.command == "recovery-status":
            result = recovery.recovery_status(args.state)
        else:
            result = recovery.activate(args.state, args.source_state, args.checkpoint, trusted_sha256=args.trusted_sha256)
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
    except subprocess.TimeoutExpired:
        print("The local dependency did not respond before its deadline; retained work was not retried.", file=sys.stderr)
        raise SystemExit(1)
