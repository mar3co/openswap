"""Operator-only reference service CLI. No secrets are read from remote callers."""
import argparse
import os
from pathlib import Path
import sqlite3
import sys

from openswap.worker.protocol import ProtocolError
from openswap.worker.refserver import ControlStore, make_server


def main(argv=None):
    parser = argparse.ArgumentParser(prog="openswap worker refserver")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("serve", "pair-code", "revoke"):
        command = commands.add_parser(name)
        command.add_argument("--database", type=Path, required=True, help="SQLite file in an owner-private directory")
        if name == "serve":
            command.add_argument("--host", default="127.0.0.1")
            command.add_argument("--port", type=int, default=8765)
            command.add_argument("--behind-owner-controlled-tls", action="store_true",
                                 help="non-loopback HTTP must sit behind owner-controlled TLS termination on a private backend link")
        if name == "revoke":
            command.add_argument("worker_id")
    args = parser.parse_args(argv)
    previous_umask = os.umask(0o077)  # SQLite journal/WAL sidecars must stay owner-private too
    try:
        store = ControlStore(args.database)
        if args.command == "pair-code":
            print(store.issue_code())
        elif args.command == "revoke":
            store.revoke(args.worker_id)
            print("Device revoked.")
        else:
            with make_server(store, args.host, args.port,
                             behind_owner_controlled_tls=args.behind_owner_controlled_tls) as server:
                print(f"Reference server listening on {args.host}:{server.server_port}", flush=True)
                try:
                    server.serve_forever(poll_interval=0.2)
                except KeyboardInterrupt:
                    pass
        return 0
    except (OSError, ValueError, ProtocolError, sqlite3.Error):
        print("Reference service unavailable; check private database permissions and TLS bind policy.", file=sys.stderr)
        return 1
    finally:
        os.umask(previous_umask)
