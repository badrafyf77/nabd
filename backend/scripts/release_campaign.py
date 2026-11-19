"""Run one specifically approved NABD campaign or summarize previously recorded runs.

Run 1, run 2, run 3 and the labeled evaluator check are separate invocations.
Use an operator-only results directory outside the repository for JSONL and logs.
"""
from __future__ import annotations

import argparse
import json
import os
from collections import Counter, defaultdict
from pathlib import Path
import subprocess
import sys

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "code"))
from nabd.runtime.quality_cli import cost, percentiles  # noqa: E402
from nabd.settings import QualitySettings  # noqa: E402

SCENARIO_FILES = ("tests/nabd/test_multilingual_scenarios.py", "tests/nabd/test_release_challenges.py")
LABEL_FILE = "tests/nabd/test_release_evaluator.py"


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=BACKEND, text=True).strip()


def run(name: str, results: Path, tests: tuple[str, ...]) -> int:
    if _git("status", "--porcelain", "--", "code", "tests", "scripts", "requirements-text.txt"):
        sys.exit("Commit the runtime and test version before the campaign.")
    version = _git("rev-parse", "HEAD")
    if not results.exists():
        results.mkdir(mode=0o700, parents=True)
    for p in results.glob("*.version"):
        if p.read_text().strip() != version:
            sys.exit("Use a fresh results directory for a different version.")
    destination = results / f"{name}.jsonl"
    version_file = results / f"{name}.version"
    if destination.exists() or version_file.exists():
        sys.exit("This invocation already exists. Preserve that evidence. A rerun needs fresh approval.")
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    version_file.write_text(version + "\n")
    env = {**os.environ, "NABD_CAMPAIGN_JSONL": str(destination), "LANGSMITH_TRACING": "false"}
    # An explicit CLI -m overrides the default offline pytest marker.
    log = results / f"{name}.log"
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        status = subprocess.run([sys.executable, "-m", "pytest", "-m", "llm", "-q", *tests],
                                cwd=BACKEND, env=env, stdout=output, stderr=subprocess.STDOUT,
                                check=False).returncode
    print(f"{name}: pytest exited {status}. Private results saved to {destination}.")
    return status


def _read(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def summarize(results: Path) -> dict:
    versions = {p.read_text().strip() for p in results.glob("*.version")}
    if len(versions) != 1:
        sys.exit("Expected one frozen version in the results directory.")
    forms, turns, scenario_results, evaluations = defaultdict(Counter), [], [], Counter()
    for number in range(1, 4):
        rows = _read(results / f"run-{number}.jsonl")
        outcomes = {row["node"].rsplit("[", 1)[1].removesuffix("]"): row["outcome"]
                    for row in rows if row["kind"] == "scenario"}
        for scenario, outcome in outcomes.items():
            forms[scenario.split("-", 1)[0]][outcome] += 1
        scenario_results.append({"run": number, "completed": len(outcomes),
                                 "outcomes": dict(Counter(outcomes.values())),
                                 "failed": sorted(key for key, val in outcomes.items() if val != "passed")})
        run_grade = defaultdict(set)
        for row in rows:
            if row["kind"] == "turn":
                turns.append(row)
                grade = (row["grade"] or {}).get("overall", "missing")
                run_grade[row["scenario"]].add(grade)
        for scenario, outcome in outcomes.items():
            grades = run_grade.get(scenario, {"missing"})
            bucket = "review" if "review" in grades else "missing" if "missing" in grades else "pass"
            evaluations[f"scenario_{outcome}__evaluator_{bucket}"] += 1
    tokens = defaultdict(Counter)
    counts = Counter()
    for turn in turns:
        for agent, usage in turn["tokens"].items():
            tokens[agent].update(usage)
            counts[agent] += 1
    configured = QualitySettings()
    judged = [row for row in turns if row["case_ok"] is not None]
    labels = {row["name"]: row for row in _read(results / "labels.jsonl") if row["kind"] == "label"}
    label_counts = Counter(f'expected_{str(row["expected_flag"]).lower()}__flagged_{str(row["flagged"]).lower()}'
                           for row in labels.values())
    complete = all(item["completed"] == 60 for item in scenario_results)
    return {
        "version": versions.pop(), "runs": scenario_results, "all_three_complete": complete,
        "forms": {form: {**counts, "target_met": (counts["passed"] >= 27 and counts.total() == 30)
                        if complete else None} for form, counts in sorted(forms.items())},
        "case_accuracy": {"passed": sum(row["case_ok"] for row in judged), "assessed_turns": len(judged)},
        "turn_ms": percentiles([t["duration_ms"] for t in turns]),
        "first_text_ms": percentiles([t["first_text_ms"] for t in turns if t["first_text_ms"] is not None]),
        "evaluations_vs_scenarios": dict(evaluations),
        "labeled_evaluator": {"recorded": len(labels), "outcomes": dict(label_counts)},
        "tokens": {agent: {"total": dict(total), "turns_with_usage": counts[agent],
                           "mean_per_turn_with_usage": {key: round(value / counts[agent], 2)
                                                        for key, value in total.items()},
                           "estimated_usd": cost(total, configured)} for agent, total in sorted(tokens.items())},
        "manual_review": "Review fixture replies (including both Darija scripts) in local run JSONL files.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", type=Path, required=True)
    actions = parser.add_subparsers(dest="action", required=True)
    campaign = actions.add_parser("run", help="one approved run of the 60 multilingual scenarios")
    campaign.add_argument("--number", type=int, choices=(1, 2, 3), required=True)
    actions.add_parser("labels", help="one approved run of the 12 labeled evaluator calls")
    report = actions.add_parser("report", help="summarize recorded results without model calls")
    report.add_argument("--output", type=Path, default=BACKEND.parent / "docs/nabd/evidence/phase7-campaign.json")
    args = parser.parse_args()
    results = args.results_dir.resolve()
    if args.action == "run":
        sys.exit(run(f"run-{args.number}", results, SCENARIO_FILES))
    if args.action == "labels":
        sys.exit(run("labels", results, (LABEL_FILE,)))
    args.output.parent.mkdir(exist_ok=True, parents=True)
    args.output.write_text(json.dumps(summarize(results), indent=2, ensure_ascii=False) + "\n")
    print(args.output)


if __name__ == "__main__":
    main()
