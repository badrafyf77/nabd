"""Maintenance commands for the text application."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from nabd.migrations import migrate
from nabd.settings import Settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m nabd")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="apply application and LangGraph migrations")
    from nabd.read_mcp import cli as read_mcp_cli
    from nabd.salesforce import cli as salesforce_cli
    from nabd.write_mcp import cli as write_mcp_cli

    salesforce_cli.add_parser(commands)
    read_mcp_cli.add_parser(commands)
    write_mcp_cli.add_parser(commands)
    from nabd.runtime import staff_cli
    staff_cli.add_parser(commands)
    args = parser.parse_args()
    quiet = args.command in ("salesforce", "staff-sync") or (
        args.command in ("read-mcp", "write-mcp") and args.action in ("prompt", "smoke"))
    logging.basicConfig(level=logging.WARNING if quiet else logging.INFO)
    if args.command == "staff-sync":
        sys.exit(asyncio.run(staff_cli.run(args.target_org, args.samples)))
    if args.command == "salesforce":
        sys.exit(salesforce_cli.run(args))
    if args.command == "read-mcp":
        sys.exit(read_mcp_cli.run(args))
    if args.command == "write-mcp":
        sys.exit(write_mcp_cli.run(args))
    asyncio.run(migrate(Settings()))
    print("NABD application and LangGraph migrations are current.")


if __name__ == "__main__":
    main()
