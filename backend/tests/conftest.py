import os


def pytest_configure(config):
    """Keep the free unit tests out of LangSmith. Scenario runs (-m llm) use your .env setting."""
    if "llm" not in (config.option.markexpr or "") or "not llm" in (config.option.markexpr or ""):
        os.environ["LANGSMITH_TRACING"] = "false"


def pytest_terminal_summary(terminalreporter):
    """Print a pass-rate table after the scenario tests."""
    try:
        from test_scenarios import RESULTS
    except ImportError:
        return
    if not RESULTS:
        return

    write = terminalreporter.write_line
    write("")
    write("Scenario pass rates")
    write(f"{'scenario':<32} {'runs passed':>12}")
    for name, runs in sorted(RESULTS.items()):
        write(f"{name:<32} {sum(runs):>5} / {len(runs):<5}")
    total_runs = sum(len(runs) for runs in RESULTS.values())
    passed_runs = sum(sum(runs) for runs in RESULTS.values())
    passed_scenarios = sum(all(runs) for runs in RESULTS.values())
    write(f"{passed_scenarios}/{len(RESULTS)} scenarios passed every run, {passed_runs}/{total_runs} runs passed")


def pytest_runtest_logreport(report):
    path = os.environ.get("NABD_CAMPAIGN_JSONL")
    if not path or report.when != "call":
        return
    import json
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(json.dumps({"kind": "scenario", "node": report.nodeid, "outcome": report.outcome}) + "\n")
