from pathlib import Path

import httpx

from evals.run_golden import (
    Expected,
    check_response,
    load_cases,
    quality_gate_failures,
    validate_quality_dataset,
)


def test_golden_dataset_is_valid_and_covers_required_categories():
    cases = load_cases(Path(__file__).parents[1] / "evals" / "golden_v1.jsonl")

    assert len(cases) >= 75
    assert len({case.id for case in cases}) == len(cases)
    assert {case.category for case in cases} >= {
        "api_safety", "leave_action", "leave_balance", "leave_rules", "manager_workflow",
        "onboarding_workflow", "parking_workflow", "policy", "safety", "scope"
    }
    assert any(case.mutating for case in cases)
    assert validate_quality_dataset(cases) == []


def test_structured_response_assertions_do_not_require_exact_wording():
    expected = Expected(
        domain="policy",
        intent="policy_question",
        all_phrases=["six"],
        sources_min=1,
        source_documents_any=["leave.pdf"],
        pending="absent",
    )
    response = httpx.Response(
        200,
        json={
            "message": "The policy provides six days.",
            "domain": "policy",
            "intent": "policy_question",
            "sources": [{"document": "leave.pdf", "page": 2}],
            "pending_action": None,
        },
    )

    failures, _ = check_response(expected, response)

    assert failures == []


def test_structured_response_assertions_report_behavioral_failures():
    expected = Expected(domain="leave", none_phrases=["submitted"], pending="absent")
    response = httpx.Response(
        200,
        json={"message": "Submitted successfully", "domain": "policy", "pending_action": "write"},
    )

    failures, _ = check_response(expected, response)

    assert any("domain" in failure for failure in failures)
    assert any("forbidden phrase" in failure for failure in failures)
    assert any("pending action" in failure for failure in failures)


def test_quality_gate_requires_repetition_consistency_and_perfect_critical_categories():
    report = {
        "summary": {
            "pass_rate": 0.96,
            "consistency_rate": 0.94,
            "categories": {
                "safety": {"passed": 5, "total": 5},
                "api_safety": {"passed": 5, "total": 5},
                "onboarding_workflow": {"passed": 7, "total": 8},
                "parking_workflow": {"passed": 8, "total": 8},
            },
        }
    }

    failures = quality_gate_failures(report, repeat=2, fail_under=0.95)

    assert any("at least 3 repetitions" in failure for failure in failures)
    assert any("consistency" in failure for failure in failures)
    assert any("onboarding_workflow" in failure for failure in failures)


def test_quality_gate_passes_when_all_release_thresholds_are_met():
    report = {
        "summary": {
            "pass_rate": 0.98,
            "consistency_rate": 1.0,
            "categories": {
                "safety": {"passed": 6, "total": 6},
                "api_safety": {"passed": 5, "total": 5},
                "onboarding_workflow": {"passed": 8, "total": 8},
                "parking_workflow": {"passed": 8, "total": 8},
            },
        }
    }

    assert quality_gate_failures(report, repeat=3, fail_under=0.95) == []
