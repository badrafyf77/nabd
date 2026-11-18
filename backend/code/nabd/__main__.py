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
    from nabd.salesforce import cli as salesforce_cli

    salesforce_cli.add_parser(commands)
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING if args.command == "salesforce" else logging.INFO)
    if args.command == "salesforce":
        sys.exit(salesforce_cli.run(args))
    asyncio.run(migrate(Settings()))
    print("NABD application and LangGraph migrations are current.")


if __name__ == "__main__":
    main()
