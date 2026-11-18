"""Maintenance commands for the text application."""
from __future__ import annotations

import argparse
import asyncio
import logging

from nabd.migrations import migrate
from nabd.settings import Settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m nabd")
    parser.add_argument("command", choices=["migrate"])
    parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(migrate(Settings()))
    print("NABD application and LangGraph migrations are current.")


if __name__ == "__main__":
    main()
