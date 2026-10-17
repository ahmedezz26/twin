"""Evals: ask the real model the golden questions in cases.yaml and grade how the twin behaves.

Unit tests (tests/) prove the code works with a fake LLM. Evals check the *answers*: the same code path
as production (server.call_llm), the real model and prompt, and fakes only at the boundaries (Telegram,
DNS). They cost money and need the network, so they are not part of pytest.

Run from the repo root (needs EVALS_OPENROUTER_API_KEY in .env or the environment):
    uv run --directory backend python -m evals.run                          # every case, 3 runs each
    uv run --directory backend python -m evals.run --only grounded --repeats 1
"""

import argparse
import itertools
import json
import os
import re
import shutil
import statistics
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

import config

if TYPE_CHECKING:
    from openai.types.chat import ChatCompletion, ChatCompletionMessageParam

    from server import StoredMessage
    from tools import ToolResult

EVALS_DIR = Path(__file__).parent
CASES_FILE = EVALS_DIR / "cases.yaml"
FIXTURE_KNOWLEDGE = EVALS_DIR / "fixtures" / "knowledge"
REPORTS_DIR = EVALS_DIR / "reports"
TFVARS = config.REPO_DIR / "terraform" / "terraform.tfvars"

# The fake DNS check accepts only these domains; any other email domain "doesn't receive email"
REAL_EMAIL_DOMAINS = {"gmail.com", "outlook.com", "valeo.com"}


class EvalSettings(BaseSettings):
    """A key of its own, so eval runs can never use up the live twin's OpenRouter budget"""

    model_config = SettingsConfigDict(env_file=config.REPO_DIR / ".env", extra="ignore")

    evals_openrouter_api_key: SecretStr | None = None


class Turn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str


class Case(BaseModel):
    """One golden question. extra="forbid": a typo like `must_inclue_any` fails loudly instead of silently grading nothing"""

    model_config = ConfigDict(extra="forbid")

    id: str
    category: str
    question: str
    history: list[Turn] = []  # earlier messages, for multi-turn cases
    critical: bool = False  # one failed run of a critical case fails the whole eval
    must_call: list[str] = []
    must_not_call: list[str] = []
    must_include_any: list[str] = []  # case-insensitive; at least one must appear
    must_not_include: list[str] = []  # case-insensitive; none may appear
    notes: str = ""


class RunResult(BaseModel):
    case_id: str
    answer: str
    tool_calls: list[dict[str, Any]]
    telegram_messages: list[str]
    failures: list[str]
    cost: float
    seconds: float

    @property
    def passed(self) -> bool:
        return not self.failures


class _Recorder(threading.local):
    """What the current run did. Thread-local: runs in parallel threads never mix their records."""

    def reset(self) -> None:
        self.tool_calls: list[dict[str, Any]] = []
        self.telegram: list[str] = []
        self.cost = 0.0


_current = _Recorder()


def production_model() -> tuple[str, str]:
    """The model and reasoning effort the live twin uses, so evals test what actually ships"""
    found = dict(
        re.findall(r'^(openrouter_model|openrouter_reasoning_effort)\s*=\s*"([^"]*)"', TFVARS.read_text(), re.MULTILINE)
    )
    return found["openrouter_model"], found["openrouter_reasoning_effort"]


def load_cases(only: str | None) -> list[Case]:
    cases = [Case.model_validate(item) for item in yaml.safe_load(CASES_FILE.read_text(encoding="utf-8"))]
    ids = [case.id for case in cases]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise SystemExit(f"Duplicate case ids in {CASES_FILE.name}: {sorted(duplicates)}")
    if only:
        cases = [case for case in cases if only in (case.id, case.category)]
    return cases


def configure(api_key: SecretStr, model: str, effort: str, workdir: Path) -> None:
    """Point the twin at a throwaway folder and the eval key, then put fakes at the boundaries.

    Must run before server is imported: server reads the settings while it is being imported.
    """
    knowledge_dir = workdir / "knowledge"
    shutil.copytree(FIXTURE_KNOWLEDGE, knowledge_dir)  # a fixed copy, so pending questions never touch the repo
    config._settings = config.Settings(
        _env_file=None,  # never read the twin's own .env values (its key, Telegram token...)
        openrouter_api_key=api_key,
        openrouter_model=model,
        openrouter_reasoning_effort=effort,  # type: ignore[arg-type]  # validated by Settings at runtime
        memory_dir=workdir / "memory",
        knowledge_dir=knowledge_dir,
    )

    import server
    import tools

    message_ids = itertools.count(1000)

    def fake_send_telegram(text: str, reply_to_message_id: int | None = None) -> int | None:
        _current.telegram.append(text)
        return next(message_ids)

    def fake_email_domain_accepts_mail(domain: str) -> bool:
        return domain.lower() in REAL_EMAIL_DOMAINS

    tools.send_telegram = fake_send_telegram  # the chat path sends through tools; only the webhook uses server's copy
    tools.email_domain_accepts_mail = fake_email_domain_accepts_mail

    def recording(name: str, tool: Callable[..., "ToolResult"]) -> Callable[..., "ToolResult"]:
        def wrapper(**kwargs: Any) -> "ToolResult":
            call: dict[str, Any] = {"name": name, "arguments": {k: v for k, v in kwargs.items() if k != "session_id"}}
            _current.tool_calls.append(call)  # recorded before running, so a call with bad arguments still shows
            result = tool(**kwargs)
            call["result"] = result
            return result

        return wrapper

    for name, tool in list(tools.tool_map.items()):
        tools.tool_map[name] = recording(name, tool)

    real_complete = server.complete

    def metered_complete(
        messages: list["ChatCompletionMessageParam"], tool_choice: Literal["auto", "none"] = "auto"
    ) -> "ChatCompletion":
        response = real_complete(messages, tool_choice)
        if response.usage is not None:
            # OpenRouter adds the exact price of every call to the usage object
            _current.cost += float((response.usage.model_extra or {}).get("cost") or 0.0)
        return response

    server.complete = metered_complete


def grade(case: Case, answer: str, tool_calls: list[dict[str, Any]]) -> list[str]:
    """Deterministic checks: an empty list means the run passed"""
    failures: list[str] = []
    called = {call["name"] for call in tool_calls}
    text = answer.lower()
    failures += [f"did not call {tool}" for tool in case.must_call if tool not in called]
    failures += [f"called {tool}" for tool in case.must_not_call if tool in called]
    if case.must_include_any and not any(word.lower() in text for word in case.must_include_any):
        failures.append(f"mentions none of {case.must_include_any}")
    failures += [f'contains "{phrase}"' for phrase in case.must_not_include if phrase.lower() in text]
    return failures


def run_once(case: Case) -> RunResult:
    from fastapi import HTTPException

    import server

    _current.reset()
    now = datetime.now(UTC).isoformat()
    history: list[StoredMessage] = [
        {"role": turn.role, "content": turn.content, "timestamp": now} for turn in case.history
    ]
    start = time.monotonic()
    try:
        answer = server.call_llm(history, case.question, session_id=str(uuid.uuid4()))
        failures = grade(case, answer, _current.tool_calls)
    except HTTPException as e:
        answer, failures = "", [f"error {e.status_code}: {e.detail}"]
    return RunResult(
        case_id=case.id,
        answer=answer,
        tool_calls=_current.tool_calls,
        telegram_messages=_current.telegram,
        failures=failures,
        cost=_current.cost,
        seconds=round(time.monotonic() - start, 1),
    )


def summarize(cases: list[Case], results: list[RunResult], model: str, effort: str) -> dict[str, Any]:
    by_case = {case.id: [r for r in results if r.case_id == case.id] for case in cases}
    rows: list[dict[str, Any]] = []
    for case in cases:
        runs = by_case[case.id]
        failed = [r for r in runs if not r.passed]
        rows.append(
            {
                "id": case.id,
                "category": case.category,
                "critical": case.critical,
                "passed": len(runs) - len(failed),
                "runs": len(runs),
                "first_failure": failed[0].failures[0] if failed else "",
            }
        )
    passed_runs = sum(r.passed for r in results)
    return {
        "date": datetime.now(UTC).isoformat(timespec="seconds"),
        "model": model,
        "reasoning_effort": effort,
        "pass_rate": round(passed_runs / len(results), 3),
        "passed_runs": passed_runs,
        "total_runs": len(results),
        "critical_failures": [row["id"] for row in rows if row["critical"] and row["passed"] < row["runs"]],
        "cost_usd": round(sum(r.cost for r in results), 4),
        "median_seconds": statistics.median(r.seconds for r in results),
        "max_seconds": max(r.seconds for r in results),
        "cases": rows,
    }


def markdown(summary: dict[str, Any]) -> str:
    lines = [
        f"### Evals: {summary['passed_runs']}/{summary['total_runs']} runs passed ({summary['pass_rate']:.0%})",
        "",
        (
            f"Model `{summary['model']}` at `{summary['reasoning_effort']}` · cost ${summary['cost_usd']:.4f} · "
            f"median {summary['median_seconds']} s, max {summary['max_seconds']} s"
        ),
        "",
        "| Case | Category | Passed | First failure |",
        "|---|---|---|---|",
    ]
    for row in summary["cases"]:
        mark = "✅" if row["passed"] == row["runs"] else ("❌" if row["critical"] else "⚠️")
        name = f"{row['id']} (critical)" if row["critical"] else row["id"]
        lines.append(f"| {mark} {name} | {row['category']} | {row['passed']}/{row['runs']} | {row['first_failure']} |")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", help="run one case id or one category")
    parser.add_argument("--repeats", type=int, default=3, help="runs per case: the model answers differently each time")
    parser.add_argument("--workers", type=int, default=4, help="runs in parallel")
    parser.add_argument("--min-pass-rate", type=float, default=0.8, help="fail below this share of passed runs")
    parser.add_argument("--model", help="override the production model, e.g. to compare a candidate")
    parser.add_argument("--effort", help="override the production reasoning effort")
    args = parser.parse_args()

    api_key = EvalSettings().evals_openrouter_api_key
    if api_key is None:
        print("EVALS_OPENROUTER_API_KEY is not set (repo-root .env locally, a GitHub secret in CI)")
        return 2
    prod_model, prod_effort = production_model()
    model, effort = args.model or prod_model, args.effort or prod_effort

    cases = load_cases(args.only)
    if not cases:
        print(f"No case or category named {args.only!r}")
        return 2

    with tempfile.TemporaryDirectory(prefix="twin-evals-") as workdir:
        configure(api_key, model, effort, Path(workdir))
        jobs = [case for case in cases for _ in range(args.repeats)]
        print(f"Running {len(cases)} cases x {args.repeats} = {len(jobs)} runs on {model} ({effort})...")
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            results = list(pool.map(run_once, jobs))

    summary = summarize(cases, results, model, effort)
    report = markdown(summary)
    print("\n" + report)

    REPORTS_DIR.mkdir(exist_ok=True)
    path = REPORTS_DIR / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps({"summary": summary, "runs": [r.model_dump() for r in results]}, indent=2) + "\n")
    print(f"\nFull report with every answer: {path.relative_to(config.REPO_DIR)}")
    if step_summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(step_summary, "a", encoding="utf-8") as f:
            f.write(report + "\n")

    if summary["critical_failures"]:
        print(f"FAILED: critical cases failed: {summary['critical_failures']}")
        return 1
    if summary["pass_rate"] < args.min_pass_rate:
        print(f"FAILED: pass rate {summary['pass_rate']:.0%} is below {args.min_pass_rate:.0%}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
