"""Runs the two assignment test cases against a running server and prints the agent's work.

Usage:  python scripts/run_demo.py            (both cases)
        python scripts/run_demo.py complex    (one case)
"""
import json
import sys
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8000"

CASES = {
    "standard": (
        "Create meeting minutes for our Q3 product review held on 15 October 2026. "
        "Attendees: Priya (Product Manager), Arjun (Engineering Lead), Neha (Sales Head). "
        "Decisions: launch the mobile app on 15 November; move the analytics dashboard to Q1 2027. "
        "Action items: Arjun to finalise the payments API by 30 October; "
        "Neha to brief our top 10 clients on the launch by 5 November."
    ),
    "complex": (
        "We need a plan to move our CRM to the cloud. Budget is tight but leadership wants it done "
        "in 6 weeks with zero downtime. Not sure how many users we have. "
        "Make something I can show the CTO."
    ),
}


def run(name: str, request: str) -> None:
    print(f"\n{'=' * 80}\n{name.upper()} REQUEST:\n{request}\n{'=' * 80}")
    r = httpx.post(f"{BASE}/agent", json={"request": request}, timeout=600)
    if r.status_code != 200:
        print(f"Error {r.status_code}: {r.text}")
        return
    body = r.json()
    i = body["interpretation"]
    print(f"\nUnderstood as: {i['doc_type']} - '{i['title']}' for {i['audience']}")
    for label in ("missing_info", "conflicts", "assumptions"):
        for item in i[label]:
            print(f"  [{label}] {item}")

    if body.get("key_decisions"):
        print("\nKey decisions (shared by all section writers):")
        for d in body["key_decisions"]:
            print(f"  * {d}")

    print("\nAgent-generated TODO list:")
    for t in body["plan"]:
        mark = {"done": "x", "failed": "!"}.get(t["status"], " ")
        print(f"  [{mark}] {t['id']}. {t['section_title']:<38} tool={t['tool']}")

    ref = body["reflection"]
    if ref.get("skipped"):
        print(f"\nSelf-check SKIPPED: {ref['skipped_reason']}")
    else:
        print(f"\nSelf-check: score {ref['initial_score']}/10"
              + (f" -> {ref['final_score']}/10 after revising {ref['sections_revised']}" if ref["sections_revised"] else ""))
    for issue in ref["issues_found"]:
        print(f"  - {issue['section_title']}: {issue['problem']}")
    for issue in ref.get("remaining_issues", []):
        print(f"  ! still open after revision - {issue['section_title']}: {issue['problem']}")

    u = body["usage"]
    print(f"\nUsage: {u['llm_calls']} LLM calls, {u['api_requests']} API requests "
          f"({u.get('api_errors', 0)} provider errors), "
          f"{u['prompt_tokens'] + u['completion_tokens']:,} tokens  (models: {body['models']})")
    print(f"Summary: {body['summary']}")
    doc = httpx.get(BASE + body["document_url"], timeout=60)
    out = Path("outputs") / f"demo_{name}.docx"
    out.parent.mkdir(exist_ok=True)
    out.write_bytes(doc.content)
    print(f"Document saved: {out}  ({body['total_duration_ms'] / 1000:.1f}s total)")
    Path("outputs", f"demo_{name}.json").write_text(json.dumps(body, indent=2))


if __name__ == "__main__":
    selected = sys.argv[1:] or list(CASES)
    for case in selected:
        run(case, CASES[case])
