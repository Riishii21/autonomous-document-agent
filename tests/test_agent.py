"""Offline tests: a scripted fake LLM stands in for Groq/Ollama, so these run in CI without keys."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from docx import Document
from fastapi.testclient import TestClient

from app import prompts
from app.agent import DocumentAgent
from app.llm import LLMError, OpenAICompatibleLLM, _extract_json
from app.main import app, get_agent
from app.schemas import Interpretation, Plan, Review, Section

COMPLEX_REQUEST = ("We need a plan to move our CRM to the cloud. Budget is tight but leadership wants it "
                   "done in 6 weeks with zero downtime. Not sure how many users we have.")


class FakeLLM:
    """Returns canned, schema-valid objects and records every call."""

    def __init__(self, *, accept=True, fail_section=None, fail_once=None, review_fails_first=True):
        self.calls: list[tuple[str, str]] = []
        self.accept = accept
        self.fail_section = fail_section
        self.fail_once = fail_once
        self.review_fails_first = review_fails_first
        self.reviews = 0

    def generate(self, system, user, schema):
        self.calls.append((schema.__name__, user))
        if schema is Interpretation:
            return Interpretation(
                is_document_request=self.accept, rejection_reason=None if self.accept else "Not a document",
                doc_type="project_plan", title="CRM Cloud Migration Plan", audience="CTO",
                objective="Migrate CRM to cloud", key_facts=["6-week deadline", "zero downtime"],
                missing_info=["number of CRM users"], conflicts=["tight budget vs 6 weeks vs zero downtime"],
                assumptions=["~500 users", "SaaS-compatible data model"])
        if schema is Plan:
            return Plan(reasoning="Standard project plan", tasks=[
                {"id": 1, "description": "Summary", "tool": "executive_summary", "section_title": "Executive Summary"},
                {"id": 2, "description": "Scope", "tool": "draft_section", "section_title": "Scope and Approach"},
                {"id": 3, "description": "Timeline", "tool": "build_table", "section_title": "Timeline"},
                {"id": 4, "description": "Risks", "tool": "draft_section", "section_title": "Risks"},
            ])
        if schema is Section:
            title = user.split("SECTION TO WRITE: ")[1].split("\n")[0]
            if title == self.fail_section:
                raise RuntimeError("simulated tool failure")
            if title == self.fail_once:
                self.fail_once = None
                raise RuntimeError("simulated transient failure")
            if system == prompts.TABLE_SYSTEM:
                return Section(title=title, blocks=[
                    {"type": "paragraph", "text": "Phased plan (**estimates**):"},
                    {"type": "table", "headers": ["Week", "Milestone", "Owner"],
                     "rows": [[1, "Assessment"], ["3-6", "Cutover", "Infra", "extra"]]}])
            return Section(title=title, blocks=[
                {"type": "paragraph", "text": f"## Content for {title} with **bold** text."},
                {"type": "bullets", "items": ["- point one", "point two"]}])
        if schema is Review:
            self.reviews += 1
            if self.review_fails_first and self.reviews == 1:
                return Review(passed=False, score=6, issues=[
                    {"section_title": "Risks", "problem": "Conflict not resolved",
                     "fix_instruction": "Add phased trade-off"},
                    {"section_title": "Cost Estimate", "problem": "No budget section",
                     "fix_instruction": "Add an estimated budget"}])
            return Review(passed=True, score=9, issues=[])
        raise AssertionError(f"unexpected schema {schema}")


@pytest.fixture
def make_client(tmp_path):
    def _make(llm):
        app.dependency_overrides[get_agent] = lambda: DocumentAgent(llm, tmp_path, max_revisions=1)
        return TestClient(app)
    yield _make
    app.dependency_overrides.clear()


def test_end_to_end_plan_reflect_and_docx(make_client, tmp_path, monkeypatch):
    llm = FakeLLM()
    client = make_client(llm)
    r = client.post("/agent", json={"request": COMPLEX_REQUEST})
    assert r.status_code == 200, r.text
    body = r.json()

    tools = [t["tool"] for t in body["plan"]]
    # guard appended the assumptions register; reflection inserted the missing section before it
    assert tools[-1] == "assumptions_register"
    assert "Cost Estimate" in [t["section_title"] for t in body["plan"]]
    assert all(t["status"] == "done" for t in body["plan"])

    refl = body["reflection"]
    assert refl["initial_score"] == 6 and refl["final_score"] == 9
    assert {"Risks", "Cost Estimate", "Executive Summary"} <= set(refl["sections_revised"])
    assert body["status"] == "completed"
    assert [s["step"] for s in body["execution_log"]] == ["analyze", "plan", "execute", "reflect", "compile_docx"]

    # content sections ran (concurrently, any order) before the deferred executive summary
    section_calls = [u.split("SECTION TO WRITE: ")[1].split("\n")[0] for n, u in llm.calls if n == "Section"]
    assert set(section_calls[:3]) == {"Scope and Approach", "Timeline", "Risks"}
    assert section_calls[3] == "Executive Summary"

    # download + inspect the .docx
    monkeypatch.setattr("app.main.settings", SimpleNamespace(output_dir=tmp_path))
    d = client.get(body["document_url"])
    assert d.status_code == 200
    doc = Document(tmp_path / f"{body['request_id']}.docx")
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "CRM Cloud Migration Plan" in text
    assert "**" not in text and "## " not in text and "- point one" not in text  # markdown stripped
    assert any("~500 users" in c.text for t in doc.tables for row in t.rows for c in row.cells)
    headings = [p.text for p in doc.paragraphs if p.style.name == "Heading 1"]
    assert headings == ["Executive Summary", "Scope and Approach", "Timeline", "Risks",
                        "Cost Estimate", "Assumptions and Open Questions"]


def test_tool_failure_is_isolated(make_client):
    client = make_client(FakeLLM(fail_section="Timeline", review_fails_first=False))
    body = client.post("/agent", json={"request": COMPLEX_REQUEST}).json()
    timeline = next(t for t in body["plan"] if t["section_title"] == "Timeline")
    assert timeline["status"] == "failed" and "simulated" in timeline["error"]
    assert body["status"] == "completed_with_warnings"


def test_transient_failure_recovered_on_retry(make_client):
    client = make_client(FakeLLM(fail_once="Timeline", review_fails_first=False))
    body = client.post("/agent", json={"request": COMPLEX_REQUEST}).json()
    assert all(t["status"] == "done" for t in body["plan"])
    execute = next(s for s in body["execution_log"] if s["step"] == "execute")
    assert "recovered on retry" in execute["detail"] and "Timeline" in execute["detail"]
    assert body["status"] == "completed"


def test_numeric_table_cells_are_coerced():
    s = Section(title="Budget", blocks=[{"type": "table", "headers": ["Item", 2026],
                                         "rows": [["Licences", 2500], ["Contingency", None]]}])
    assert s.blocks[0].headers == ["Item", "2026"]
    assert s.blocks[0].rows == [["Licences", "2500"], ["Contingency", ""]]


def test_guardrail_rejects_non_document_request(make_client):
    r = make_client(FakeLLM(accept=False)).post("/agent", json={"request": "tell me a joke about cats please"})
    assert r.status_code == 422


def test_input_validation(make_client):
    client = make_client(FakeLLM())
    assert client.post("/agent", json={"request": "   hi    "}).status_code == 422
    assert client.post("/agent", json={}).status_code == 422


def test_download_rejects_bad_ids():
    client = TestClient(app)
    assert client.get("/documents/..%2F..%2Fetc").status_code in (400, 404)
    assert client.get("/documents/abcdef1234").status_code == 404


# ------------------------------------------------------------- LLM layer


def test_extract_json_handles_fences_and_chatter():
    assert _extract_json('Sure! ```json\n{"a": 1}\n``` hope that helps') == '{"a": 1}'
    assert _extract_json('Here you go: {"a": {"b": 2}} done') == '{"a": {"b": 2}}'


def _fake_completion(content):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def test_llm_retries_with_validation_feedback():
    llm = OpenAICompatibleLLM("http://x", "k", "m", json_retries=2)
    outputs = iter(['{"passed": true}', '{"passed": true, "score": 9, "issues": []}'])
    seen = []

    def create(**kw):
        seen.append(kw["messages"])
        return _fake_completion(next(outputs))

    llm._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    review = llm.generate("sys", "user", Review)
    assert review.score == 9
    assert "invalid" in seen[1][-1]["content"]  # error fed back on the retry


def _bad_request(code, failed_generation=""):
    import httpx
    from openai import BadRequestError
    resp = httpx.Response(400, request=httpx.Request("POST", "http://x"))
    return BadRequestError("bad", response=resp, body={"error": {
        "code": code, "failed_generation": failed_generation}})


def test_llm_recovers_from_provider_json_rejection():
    """Groq returns 400 json_validate_failed instead of content; we must retry, not crash."""
    llm = OpenAICompatibleLLM("http://x", "k", "m", json_retries=2)
    seen = []

    def create(**kw):
        seen.append(kw["messages"])
        if len(seen) == 1:
            raise _bad_request("json_validate_failed", '{"passed": true, "score": 9, "issues": [')
        return _fake_completion('{"passed": true, "score": 9, "issues": []}')

    llm._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    assert llm.generate("sys", "user", Review).score == 9
    assert seen[1][-2]["content"].startswith('{"passed"')  # broken output fed back for correction


def test_llm_reraises_other_bad_requests():
    llm = OpenAICompatibleLLM("http://x", "k", "m")

    def create(**kw):
        raise _bad_request("model_not_found")

    llm._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    from openai import BadRequestError
    with pytest.raises(BadRequestError):
        llm.generate("sys", "user", Review)


def test_llm_gives_up_after_retries():
    llm = OpenAICompatibleLLM("http://x", "k", "m", json_retries=1)
    llm._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kw: _fake_completion("not json at all"))))
    with pytest.raises(LLMError):
        llm.generate("sys", "user", Review)
