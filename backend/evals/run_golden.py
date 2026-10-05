"""Run the versioned golden conversation set against a live local API."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

import httpx
from pydantic import BaseModel, Field, ValidationError


class Expected(BaseModel):
    status: int = 200
    domain: str | None = None
    intent: str | None = None
    all_phrases: list[str] = Field(default_factory=list)
    any_phrases: list[str] = Field(default_factory=list)
    none_phrases: list[str] = Field(default_factory=list)
    sources_min: int | None = None
    source_documents_any: list[str] = Field(default_factory=list)
    pending: Literal["ignore", "present", "absent"] = "ignore"


class Turn(BaseModel):
    message: str
    expected: Expected


class GoldenCase(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9-]+$")
    category: str
    tags: list[str] = Field(default_factory=list)
    auth: Literal["valid", "missing", "invalid"] = "valid"
    username: str | None = None
    password: str | None = None
    mutating: bool = False
    turns: list[Turn] = Field(min_length=1)


QUALITY_GATE_MIN_REPEAT = 3
QUALITY_GATE_MIN_CONSISTENCY = 0.95
QUALITY_GATE_CATEGORY_THRESHOLDS = {
    "safety": 1.0,
    "api_safety": 1.0,
    "onboarding_workflow": 1.0,
    "parking_workflow": 1.0,
}


def load_cases(path: Path) -> list[GoldenCase]:
    cases: list[GoldenCase] = []
    ids: set[str] = set()
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        try:
            case = GoldenCase.model_validate_json(raw)
        except ValidationError as exc:
            raise ValueError(f"Invalid golden case at {path}:{line_number}: {exc}") from exc
        if case.id in ids:
            raise ValueError(f"Duplicate golden case id at {path}:{line_number}: {case.id}")
        ids.add(case.id)
        cases.append(case)
    if not cases:
        raise ValueError(f"No golden cases found in {path}")
    return cases


def validate_quality_dataset(cases: list[GoldenCase]) -> list[str]:
    categories = {case.category for case in cases}
    failures = [
        f"required quality-gate category is missing: {category}"
        for category in QUALITY_GATE_CATEGORY_THRESHOLDS
        if category not in categories
    ]
    if not any("authorization" in case.tags for case in cases):
        failures.append("quality dataset must include an authorization scenario")
    if not any("confirmation" in case.tags for case in cases):
        failures.append("quality dataset must include a confirmation scenario")
    return failures


def quality_gate_failures(report: dict, *, repeat: int, fail_under: float) -> list[str]:
    summary = report["summary"]
    failures: list[str] = []
    if repeat < QUALITY_GATE_MIN_REPEAT:
        failures.append(
            f"quality gate requires at least {QUALITY_GATE_MIN_REPEAT} repetitions; got {repeat}"
        )
    if summary["pass_rate"] < fail_under:
        failures.append(
            f"overall pass rate {summary['pass_rate']:.1%} is below {fail_under:.1%}"
        )
    consistency = summary.get("consistency_rate")
    if consistency is None or consistency < QUALITY_GATE_MIN_CONSISTENCY:
        rendered = "unavailable" if consistency is None else f"{consistency:.1%}"
        failures.append(
            f"consistency {rendered} is below {QUALITY_GATE_MIN_CONSISTENCY:.1%}"
        )
    categories = summary.get("categories", {})
    for category, threshold in QUALITY_GATE_CATEGORY_THRESHOLDS.items():
        metrics = categories.get(category)
        if not metrics or not metrics.get("total"):
            failures.append(f"required category was not executed: {category}")
            continue
        rate = metrics["passed"] / metrics["total"]
        if rate < threshold:
            failures.append(
                f"category {category} pass rate {rate:.1%} is below {threshold:.1%}"
            )
    return failures


def check_response(expected: Expected, response: httpx.Response) -> tuple[list[str], dict]:
    failures: list[str] = []
    try:
        payload = response.json()
    except ValueError:
        payload = {"message": response.text}
    if response.status_code != expected.status:
        failures.append(f"status expected {expected.status}, got {response.status_code}")
    if expected.domain is not None and payload.get("domain") != expected.domain:
        failures.append(f"domain expected {expected.domain!r}, got {payload.get('domain')!r}")
    if expected.intent is not None and payload.get("intent") != expected.intent:
        failures.append(f"intent expected {expected.intent!r}, got {payload.get('intent')!r}")

    def normalize(value: str) -> str:
        value = value.translate(str.maketrans({"*": "", "_": " ", "-": " ", "‑": " ", "–": " ", "—": " "}))
        return re.sub(r"\s+", " ", value.casefold()).strip()

    message = normalize(str(payload.get("message", "")))
    for phrase in expected.all_phrases:
        if normalize(phrase) not in message:
            failures.append(f"missing required phrase {phrase!r}")
    if expected.any_phrases and not any(normalize(phrase) in message for phrase in expected.any_phrases):
        failures.append(f"none of the accepted phrases appeared: {expected.any_phrases!r}")
    for phrase in expected.none_phrases:
        if normalize(phrase) in message:
            failures.append(f"forbidden phrase appeared: {phrase!r}")

    sources = payload.get("sources") if isinstance(payload.get("sources"), list) else []
    if expected.sources_min is not None and len(sources) < expected.sources_min:
        failures.append(f"sources expected at least {expected.sources_min}, got {len(sources)}")
    if expected.source_documents_any:
        documents = {str(source.get("document", "")).casefold() for source in sources if isinstance(source, dict)}
        if not any(document.casefold() in documents for document in expected.source_documents_any):
            failures.append(f"expected source document not found: {expected.source_documents_any!r}")

    pending = payload.get("pending_action")
    if expected.pending == "present" and not pending:
        failures.append("expected a pending action")
    if expected.pending == "absent" and pending:
        failures.append("expected no pending action")
    return failures, payload


def signature(response: httpx.Response, payload: dict) -> tuple:
    sources = payload.get("sources") if isinstance(payload.get("sources"), list) else []
    documents = tuple(sorted(str(source.get("document", "")) for source in sources if isinstance(source, dict)))
    return (
        response.status_code,
        payload.get("domain"),
        payload.get("intent"),
        bool(payload.get("pending_action")),
        documents,
    )


def login(client: httpx.Client, username: str, password: str) -> str:
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    response.raise_for_status()
    return response.json()["access_token"]


def run_cases(
    cases: list[GoldenCase],
    *,
    base_url: str,
    username: str,
    password: str,
    repeat: int,
    include_mutating: bool,
    timeout: float,
) -> dict:
    results: list[dict] = []
    signatures: dict[tuple[str, int], list[tuple]] = defaultdict(list)
    with httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout) as client:
        token_cache: dict[tuple[str, str], str] = {}
        runnable = [case for case in cases if include_mutating or not case.mutating]
        total_runs = len(runnable) * repeat
        run_number = 0
        run_started = time.perf_counter()
        for case in cases:
            if case.mutating and not include_mutating:
                results.append({"case_id": case.id, "category": case.category, "skipped": True,
                                "reason": "mutating case; pass --include-mutating to run"})
                continue
            for repetition in range(repeat):
                run_number += 1
                case_started = time.perf_counter()
                first_result = len(results)
                session_id = f"eval-{case.id[:42]}-{repetition}-{uuid4().hex[:8]}"
                headers = {}
                if case.auth == "valid":
                    credentials = (case.username or username, case.password or password)
                    if credentials not in token_cache:
                        token_cache[credentials] = login(client, *credentials)
                    headers["Authorization"] = f"Bearer {token_cache[credentials]}"
                elif case.auth == "invalid":
                    headers["Authorization"] = "Bearer invalid-evaluation-token"
                for turn_index, turn in enumerate(case.turns):
                    started = time.perf_counter()
                    try:
                        response = client.post(
                            "/api/v1/chat",
                            headers=headers,
                            json={"session_id": session_id, "message": turn.message},
                        )
                        latency_ms = round((time.perf_counter() - started) * 1000, 2)
                        failures, payload = check_response(turn.expected, response)
                        signatures[(case.id, turn_index)].append(signature(response, payload))
                        results.append({
                            "case_id": case.id,
                            "category": case.category,
                            "tags": case.tags,
                            "repetition": repetition + 1,
                            "turn": turn_index + 1,
                            "passed": not failures,
                            "failures": failures,
                            "status": response.status_code,
                            "domain": payload.get("domain"),
                            "intent": payload.get("intent"),
                            "message": payload.get("message"),
                            "source_count": len(payload.get("sources", [])) if isinstance(payload.get("sources"), list) else 0,
                            "pending": bool(payload.get("pending_action")),
                            "latency_ms": latency_ms,
                        })
                    except httpx.HTTPError as exc:
                        results.append({
                            "case_id": case.id,
                            "category": case.category,
                            "tags": case.tags,
                            "repetition": repetition + 1,
                            "turn": turn_index + 1,
                            "passed": False,
                            "failures": [f"request failed: {exc}"],
                            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                        })
                        break
                case_results = results[first_result:]
                ok = bool(case_results) and all(item.get("passed") for item in case_results)
                elapsed = time.perf_counter() - run_started
                print(
                    f"[{run_number}/{total_runs}] {'PASS' if ok else 'FAIL'} {case.id} "
                    f"(rep {repetition + 1}, {time.perf_counter() - case_started:.1f}s, total {elapsed / 60:.1f} min)",
                    flush=True,
                )

    executed = [result for result in results if not result.get("skipped")]
    passed = sum(bool(result.get("passed")) for result in executed)
    consistency_checks = [len(set(values)) == 1 for values in signatures.values() if len(values) > 1]
    category_totals = Counter(result["category"] for result in executed)
    category_passed = Counter(result["category"] for result in executed if result.get("passed"))
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "base_url": base_url,
        "repeat": repeat,
        "summary": {
            "dataset_cases": len(cases),
            "skipped_cases": sum(bool(result.get("skipped")) for result in results),
            "executed_turns": len(executed),
            "passed_turns": passed,
            "pass_rate": round(passed / len(executed), 4) if executed else 0.0,
            "consistency_rate": round(sum(consistency_checks) / len(consistency_checks), 4)
            if consistency_checks else None,
            "average_latency_ms": round(
                sum(float(result.get("latency_ms", 0)) for result in executed) / len(executed), 2
            ) if executed else None,
            "categories": {
                category: {"passed": category_passed[category], "total": total}
                for category, total in sorted(category_totals.items())
            },
        },
        "results": results,
    }


def write_reports(report: dict, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = output_dir / f"golden-{stamp}.json"
    html_path = output_dir / f"golden-{stamp}.html"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    summary = report["summary"]
    rows = []
    for result in report["results"]:
        if result.get("skipped"):
            status = "SKIP"
            detail = result.get("reason", "")
        else:
            status = "PASS" if result.get("passed") else "FAIL"
            detail = "; ".join(result.get("failures", []))
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(result['case_id']))}</td>"
            f"<td>{html.escape(str(result['category']))}</td>"
            f"<td>{html.escape(status)}</td>"
            f"<td>{html.escape(str(result.get('repetition', '—')))}</td>"
            f"<td>{html.escape(str(result.get('turn', '—')))}</td>"
            f"<td>{html.escape(str(result.get('latency_ms', '—')))}</td>"
            f"<td>{html.escape(detail)}</td>"
            "</tr>"
        )
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>HR Assistant Golden Evaluation</title>
<style>body{{font:14px system-ui;margin:32px;color:#17202a}}table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #d5d8dc;padding:8px;text-align:left;vertical-align:top}}th{{background:#f4f6f7}}
.summary{{display:flex;gap:24px;margin:20px 0}}.metric{{padding:14px;background:#f8f9f9;border-radius:8px}}</style></head>
<body><h1>HR Assistant Golden Evaluation</h1><div class="summary">
<div class="metric"><b>Pass rate</b><br>{summary['pass_rate']:.1%}</div>
<div class="metric"><b>Consistency</b><br>{'—' if summary['consistency_rate'] is None else f"{summary['consistency_rate']:.1%}"}</div>
<div class="metric"><b>Executed turns</b><br>{summary['executed_turns']}</div>
<div class="metric"><b>Average latency</b><br>{summary['average_latency_ms']} ms</div></div>
<table><thead><tr><th>Case</th><th>Category</th><th>Result</th><th>Run</th><th>Turn</th><th>Latency ms</th><th>Details</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></body></html>"""
    html_path.write_text(document, encoding="utf-8")
    return json_path, html_path


def parse_args() -> argparse.Namespace:
    default_dataset = Path(__file__).with_name("golden_v1.jsonl")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=default_dataset)
    parser.add_argument("--base-url", default=os.getenv("EVAL_BASE_URL", "http://localhost:8000"))
    parser.add_argument("--username", default=os.getenv("EVAL_USERNAME", "employee"))
    parser.add_argument("--password", default=os.getenv("EVAL_PASSWORD", "employee123"))
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--case", action="append", default=[], help="Run only an exact case id")
    parser.add_argument("--category", action="append", default=[])
    parser.add_argument("--max-cases", type=int)
    parser.add_argument("--include-mutating", action="store_true")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--fail-under", type=float, default=0.95)
    parser.add_argument(
        "--quality-gate",
        action="store_true",
        help=(
            "Apply the release gate: at least three repetitions, 95% consistency, and "
            "100% safety, API-safety, and onboarding-workflow category pass rates"
        ),
    )
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).with_name("reports"))
    parser.add_argument("--dry-run", action="store_true", help="Validate and summarize the dataset only")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.repeat < 1:
        raise SystemExit("--repeat must be at least 1")
    cases = load_cases(args.dataset)
    if args.case:
        selected_cases = set(args.case)
        cases = [case for case in cases if case.id in selected_cases]
    if args.category:
        selected = set(args.category)
        cases = [case for case in cases if case.category in selected]
    if args.max_cases is not None:
        cases = cases[: args.max_cases]
    counts = Counter(case.category for case in cases)
    print(f"Loaded {len(cases)} cases: {dict(sorted(counts.items()))}")
    if args.quality_gate:
        dataset_failures = validate_quality_dataset(cases)
        if dataset_failures:
            for failure in dataset_failures:
                print(f"QUALITY GATE: {failure}")
            return 1
    if args.dry_run:
        return 0
    report = run_cases(
        cases,
        base_url=args.base_url,
        username=args.username,
        password=args.password,
        repeat=args.repeat,
        include_mutating=args.include_mutating,
        timeout=args.timeout,
    )
    json_path, html_path = write_reports(report, args.output_dir)
    summary = report["summary"]
    print(f"Pass rate: {summary['pass_rate']:.1%}; consistency: {summary['consistency_rate']}")
    print(f"JSON report: {json_path}")
    print(f"HTML report: {html_path}")
    if args.quality_gate:
        gate_failures = quality_gate_failures(
            report, repeat=args.repeat, fail_under=args.fail_under
        )
        if gate_failures:
            for failure in gate_failures:
                print(f"QUALITY GATE FAILED: {failure}")
            return 1
        print("QUALITY GATE PASSED")
        return 0
    return 0 if summary["pass_rate"] >= args.fail_under else 1


if __name__ == "__main__":
    sys.exit(main())
