from pathlib import Path

import httpx

from evals.run_golden import Expected, check_response, load_cases


def test_golden_dataset_is_valid_and_covers_required_categories():
    cases = load_cases(Path(__file__).parents[1] / "evals" / "golden_v1.jsonl")

    assert len(cases) >= 75
    assert len({case.id for case in cases}) == len(cases)
    assert {case.category for case in cases} >= {
        "api_safety", "leave_action", "leave_balance", "leave_rules", "policy", "safety", "scope"
    }
    assert any(case.mutating for case in cases)


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
