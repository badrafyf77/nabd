"""``python -m nabd salesforce ...``: generate, deploy, configure, seed, verify, smoke.

Deployment, configuration and seeding use the developer's ``sf`` CLI session
(``--target-org``). ``smoke`` uses only the backend's own integration
credentials from ``backend/.env.nabd``.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
from datetime import date

import httpx

from nabd.salesforce import fixtures, metadata, org, seed, smoke
from nabd.salesforce import schema as s
from nabd.salesforce.rest import CliOrgAuth, SalesforceClient
from nabd.salesforce.soql import literal
from nabd.settings import SalesforceSettings

DEFAULT_ORG = "nabd-dev"


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("salesforce", help="Salesforce metadata, seed and smoke commands")
    commands = parser.add_subparsers(dest="salesforce_command", required=True)
    commands.add_parser("generate", help="write SFDX metadata from the schema contract")
    commands.add_parser("check", help="fail if committed metadata differs from the schema contract")
    for name, text in (("deploy", "deploy metadata and run Apex tests"),
                       ("configure", "configure staff access, the integration user and its client app"),
                       ("seed", "upsert fixtures, preserving records staff edited"),
                       ("reset", "restore fixture values, overwriting staff edits to fixture records"),
                       ("verify", "compare the org with the schema contract and fixtures")):
        command = commands.add_parser(name, help=text)
        command.add_argument("--target-org", default=DEFAULT_ORG)
        if name in ("seed", "reset", "verify"):
            command.add_argument("--seed-date", type=date.fromisoformat, default=fixtures.SEED_DATE,
                                 help=f"date that fixture offsets count from (default {fixtures.SEED_DATE})")
        if name == "configure":
            command.add_argument("--create-integration-user", action="store_true",
                                 help="create the API-only integration user if it does not exist")
        if name == "reset":
            command.add_argument("--confirm", required=True, metavar="ORG_ALIAS",
                                 help="repeat the target org alias to confirm the reset")
    commands.add_parser("smoke", help="authenticated read checks as the integration user")


def run(args: argparse.Namespace) -> int:
    command = args.salesforce_command
    if command == "generate":
        print(f"Wrote {metadata.write()} generated metadata files to {metadata.SOURCE_ROOT}")
        return 0
    if command == "check":
        problems = metadata.check()
        for problem in problems:
            print(problem)
        print("Generated metadata matches the schema contract." if not problems else
              "Run: python -m nabd salesforce generate")
        return 1 if problems else 0
    if command == "deploy":
        return _deploy(args.target_org)
    if command == "smoke":
        return asyncio.run(_smoke())
    if command == "reset" and args.confirm != args.target_org:
        print("Reset not confirmed: --confirm must repeat the --target-org alias.")
        return 2
    return asyncio.run(_with_cli_org(args))


def _sf(*arguments: str, env: dict[str, str] | None = None) -> int:
    executable = shutil.which("sf")
    if executable is None:
        print("The Salesforce CLI (sf) is not installed.")
        return 1
    return subprocess.run([executable, *arguments], cwd=metadata.SALESFORCE_ROOT, env=env, check=False).returncode


def _deploy(target_org: str) -> int:
    problems = metadata.check()
    if problems:
        print("Generated metadata is out of date; run: python -m nabd salesforce generate")
        return 1
    return _sf("project", "deploy", "start", "--target-org", target_org, "--source-dir", "force-app",
               "--test-level", "RunLocalTests", "--wait", "30")


async def _with_cli_org(args: argparse.Namespace) -> int:
    auth = CliOrgAuth(args.target_org)
    async with httpx.AsyncClient(timeout=60) as http:
        client = SalesforceClient(auth, http, s.API_VERSION)
        await auth.token()
        if args.salesforce_command == "configure":
            result = await org.configure(client, org_id=auth.org_id, staff_username=auth.username,
                                         create_integration_user=args.create_integration_user)
            for change in result.changes or ["no access changes needed"]:
                print(f"- {change}")
            env = {**os.environ, s.RUN_AS_ENVIRONMENT: result.integration_username}
            code = _sf("project", "deploy", "start", "--target-org", args.target_org,
                       "--source-dir", "integration-app", "--wait", "20", env=env)
            if code != 0:
                return code
            # Deploying the OAuth policy clears its approved permission sets: approve afterwards.
            if await org.approve_client_app(client):
                print(f"- approved {s.INTEGRATION_PERMISSION_SET} for {s.EXTERNAL_CLIENT_APP}")
            print(f"Integration user: {result.integration_username}")
            print("Client app NABD_Backend runs the client credentials flow as that user.")
            return 0
        dataset = fixtures.build(args.seed_date)
        if args.salesforce_command == "verify":
            checks = await org.verify_schema(client)
            checks += await org.verify_data(client, dataset)
            checks += await org.verify_access(client, org_id=auth.org_id, staff_username=auth.username)
            return _report([(check.name, check.passed, check.detail) for check in checks])
        staff = (await client.query(f"SELECT Id FROM User WHERE Username = {literal(auth.username)}"))[0]["Id"]
        report = await seed.Seeder(client, staff_user_id=staff).run(
            seed.records(dataset), reset=args.salesforce_command == "reset")
        print(f"Seed date {dataset.seed_date}; {report.writes} record writes.")
        for sobject, counts in report.counts.items():
            print(f"  {sobject:<28} " + ", ".join(f"{action} {count}" for action, count in sorted(counts.items())))
        for key in report.preserved:
            print(f"  preserved staff-edited record {key}")
        return 0


async def _smoke() -> int:
    results = await smoke.run(SalesforceSettings())
    return _report([(result.name, result.passed, result.detail) for result in results])


def _report(rows: list[tuple[str, bool, str]]) -> int:
    for name, passed, detail in rows:
        print(f"{'PASS' if passed else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    failed = sum(not passed for _, passed, _ in rows)
    print(f"{len(rows) - failed}/{len(rows)} checks passed")
    return 1 if failed else 0

