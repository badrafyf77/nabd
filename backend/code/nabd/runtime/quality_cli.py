"""Local, database-authorized operator commands. No HTTP endpoints."""
from __future__ import annotations

import argparse
import json
import math
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from statistics import mean, median

from nabd.agents.quality import CHECKS
from nabd.database import open_database
from nabd.runtime.quality_store import QualityStore
from nabd.runtime.store import Store
from nabd.settings import QualitySettings, Settings

TOKEN_FIELDS = ("input", "cached_input", "output", "reasoning")


def percentiles(values: list[float]) -> dict:
    if not values:
        return {"samples": 0, "p50": None, "p95": None}
    return {"samples": len(values), "p50": round(median(values), 3),
            "p95": round(sorted(values)[math.ceil(len(values) * 0.95) - 1], 3)}


def cost(usage: dict, settings: QualitySettings) -> float | None:
    if settings.price_input is None or settings.price_output is None:
        return None
    cached = settings.price_cached_input if settings.price_cached_input is not None else settings.price_input
    input_tokens, cached_tokens = usage.get("input", 0), usage.get("cached_input", 0)
    return (settings.price_input * (input_tokens - cached_tokens) + cached * cached_tokens +
            settings.price_output * usage.get("output", 0)) / 1_000_000


def _since(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def add_parser(commands: argparse._SubParsersAction) -> None:
    quality = commands.add_parser("quality", help="private database reports and diagnostic traces")
    actions = quality.add_subparsers(dest="action", required=True)
    report = actions.add_parser("report", help="aggregate quality grades, summary corrections, latency and usage")
    report.add_argument("--since", type=_since, help="ISO date or timestamp (UTC if no offset)")
    show = actions.add_parser("show", help="print one restricted trace and grade, including customer data")
    show.add_argument("run_id", type=uuid.UUID)


async def _show(store: QualityStore, run_id: uuid.UUID) -> dict | None:
    async with store.pool.connection() as conn:
        row = await (await conn.execute("""
            SELECT id, status, attempt, finished_at FROM nabd.runs WHERE id = %s
            """, (run_id,))).fetchone()
        if row is None:
            return None
        row["review"] = await (await conn.execute("""
            SELECT status, attempts, grade, usage, error_code FROM nabd.quality_reviews WHERE run_id = %s
            """, (run_id,))).fetchone()
        row["summary_revisions"] = await (await conn.execute("""
            SELECT s.operation_id, s.source_command, s.status, s.attempts, s.result, s.error_code
            FROM nabd.summary_revisions s JOIN nabd.case_commands c ON c.operation_id = s.source_command
            WHERE c.run_id = %s ORDER BY s.operation_id
            """, (run_id,))).fetchall()
    row["trace"] = await store.trace(run_id)
    return row


async def _report(store: QualityStore, since: datetime | None, settings: QualitySettings) -> dict:
    since = since or datetime.min.replace(tzinfo=timezone.utc)
    async with store.pool.connection() as conn:
        rows = await (await conn.execute("""
            SELECT r.id, r.attempt, q.status, q.grade, r.created_at, r.finished_at,
                (SELECT min(e.created_at) FROM nabd.events e WHERE e.conversation_id = r.conversation_id
                   AND e.run_id = r.id AND e.type = 'message.delta'
                   AND e.payload->>'attempt' = r.attempt::text) AS first_text
            FROM nabd.runs r LEFT JOIN nabd.quality_reviews q ON q.run_id = r.id
            WHERE r.status = 'completed' AND r.finished_at >= %s
            """, (since,))).fetchall()
        revisions = await (await conn.execute("""
            SELECT s.status, s.error_code FROM nabd.summary_revisions s
            JOIN nabd.case_commands c ON c.operation_id = s.source_command
            JOIN nabd.runs r ON r.id = c.run_id WHERE r.finished_at >= %s
            """, (since,))).fetchall()
        usage = await (await conn.execute("""
            SELECT t.run_id, t.agent, t.usage FROM nabd.run_traces t JOIN nabd.runs r ON r.id = t.run_id
             WHERE r.finished_at >= %(since)s AND t.kind = 'model' AND t.usage IS NOT NULL
               AND t.usage <> 'null'::jsonb
            UNION ALL
            SELECT q.run_id, 'quality', q.usage FROM nabd.quality_reviews q
             JOIN nabd.runs r ON r.id = q.run_id WHERE r.finished_at >= %(since)s AND q.usage IS NOT NULL
            UNION ALL
            SELECT c.run_id, 'summary_correction', s.usage FROM nabd.summary_revisions s
             JOIN nabd.case_commands c ON c.operation_id = s.source_command
             JOIN nabd.runs r ON r.id = c.run_id WHERE r.finished_at >= %(since)s AND s.usage IS NOT NULL
            """, {"since": since})).fetchall()

    grades = {check: Counter() for check in CHECKS}
    languages, findings = defaultdict(Counter), Counter()
    totals = Counter()
    for row in rows:
        if not row["grade"]:
            continue
        grade = row["grade"]
        totals[grade["overall"]] += 1
        for check, counts in grades.items():
            counts[grade["grades"][check]] += 1
        language = grade["language"]
        key = f'{language.get("reply_language", "unknown")}/{language.get("reply_script", "unknown")}'
        languages[key][grade["overall"]] += 1
        findings.update(f'{item["check"]}/{item["severity"]}' for item in grade["findings"])
        findings.update(item["code"] for item in grade["checks"])

    by_agent = defaultdict(lambda: defaultdict(Counter))
    for row in usage:
        by_agent[row["agent"]][row["run_id"]].update(row["usage"])
    tokens = {}
    for agent, turns in by_agent.items():
        total = sum(turns.values(), Counter())
        tokens[agent] = {"turns_with_usage": len(turns), "total": {key: total[key] for key in TOKEN_FIELDS},
                         "mean_per_turn_with_usage": {key: round(mean(turn[key] for turn in turns.values()), 2)
                                                      for key in TOKEN_FIELDS},
                         "estimated_usd": cost(total, settings)}

    def rates(counts: Counter, passes: tuple[str, ...] = ("pass",)) -> dict:
        size = counts.total()
        return {"counts": dict(counts), "pass_rate": sum(counts[key] for key in passes) / size if size else None}

    return {
        "completed_turns": len(rows), "review_jobs": dict(Counter(row["status"] or "absent" for row in rows)),
        "overall": rates(totals), "grades": {key: rates(counts, ("pass", "not_needed", "none"))
                                            for key, counts in grades.items()},
        "by_reply_language_script": {key: rates(counts) for key, counts in sorted(languages.items())},
        "most_common_findings": findings.most_common(20),
        "summary_corrections": dict(Counter(row["status"] for row in revisions)),
        "summary_correction_reasons": dict(Counter(row["error_code"] for row in revisions if row["error_code"])),
        "turn_ms": percentiles([(row["finished_at"] - row["created_at"]).total_seconds() * 1000 for row in rows]),
        "first_text_ms": percentiles([(row["first_text"] - row["created_at"]).total_seconds() * 1000
                                      for row in rows if row["first_text"]]),
        "tokens_by_agent": tokens,
    }


async def run(args: argparse.Namespace) -> int:
    async with open_database(Settings()) as database:
        store = QualityStore(Store(database.pool))
        result = await (_show(store, args.run_id) if args.action == "show" else
                        _report(store, args.since, QualitySettings()))
    if result is None:
        print("Run not found.")
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, default=str))
    return 0
