# Autonomous Document Agent

A FastAPI service that takes a natural-language request, works out what document is needed, writes its own TODO list, executes it through a tool registry, checks its own work, and returns a polished Microsoft Word (.docx) file.

Runs on free LLMs: **Groq** (default, `openai/gpt-oss-120b`) or **Ollama** locally. No paid API credits needed. The model is set in `.env`, so switching models needs no code change.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                     # add your free Groq key
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000/docs for Swagger, or:

```bash
curl -X POST http://127.0.0.1:8000/agent \
  -H "Content-Type: application/json" \
  -d '{"request": "Write an SOP for onboarding new support engineers"}'
```

Run both assignment test cases (server must be running):

```bash
python scripts/run_demo.py          # writes outputs/demo_standard.docx and outputs/demo_complex.docx
```

Run the offline test suite (no API key needed, uses a scripted fake LLM):

```bash
pytest -q
```

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/agent` | Body `{"request": "..."}`. Runs the agent; returns interpretation, plan, reflection, summary, execution log and `document_url`. |
| `GET` | `/documents/{request_id}` | Downloads the generated .docx. |
| `GET` | `/health` | Provider and model in use. |

Errors: `422` invalid or non-document request (guardrail), `502` LLM could not produce valid output after retries, `500` no section could be generated.

## How the agent works

```
POST /agent
  1. analyze   LLM -> doc_type, audience, key facts, MISSING info, CONFLICTS, ASSUMPTIONS
               (guardrail: rejects requests that are not document requests)
  2. plan      LLM -> TODO list; each task = {section, tool, instructions}
               + deterministic guard: adds an assumptions register if the analysis found gaps
  3. execute   dispatch each task to a tool from the registry
               independent content sections drafted concurrently (bounded thread pool)
               failed tasks retried once; executive summary deferred until content exists
               a task that still fails is marked "failed" and the rest continue
  4. reflect   LLM reviewer scores the draft against the ORIGINAL request (1-10)
               -> rewrites flagged sections with the feedback, adds missing sections,
                  refreshes the summary, then re-scores (bounded to MAX_REVISIONS)
  5. compile   python-docx renders title block, styled headings, tables, lists,
               header and page-numbered footer (no LLM in this step)
```

**Tools** (`app/tools.py`): `draft_section`, `build_table`, `executive_summary` (LLM-backed) and `assumptions_register` (deterministic, renders exactly what the analysis recorded so assumptions can't drift).

**Design principle:** the LLM decides *what* to do (document type, structure, content, review verdict); plain Python decides *how* it runs (dispatch, ordering, failure isolation, rendering).

## Engineering improvement: Reflection / self-check

After drafting, a separate reviewer prompt compares the whole document against the original request and the analysis: are all stated facts present, are conflicts actually resolved, is anything missing for this document type, are invented figures labelled? It returns a score and per-section issues. The agent then rewrites only the flagged sections (or plans a new section if one is missing), regenerates the summary so it stays consistent, and re-scores.

Why: single-pass LLM drafts regularly drop explicit requirements, especially in long or conflicting requests. Catching this before the user does is the cheapest quality gain available. It's bounded to one revision round to cap latency and token use on the free tier.

## Reliability

- Every LLM output is parsed into a Pydantic model. Invalid JSON or a schema mismatch (e.g. a tool name not in the registry) is fed back to the model with the validation error and retried.
- Rate limits (429) and transient network errors are retried with backoff by the client.
- Groq validates JSON mode server-side and returns `400 json_validate_failed` (with the broken text attached) instead of a response. That error is caught and the broken output goes through the same self-correction retry as any other invalid output.
- Failed sections get one recovery retry; anything still missing is reported to the reviewer.
- Numeric table cells (`2500`) are coerced to strings, since Pydantic v2 rejects int-to-str by default.
- Markdown leaking into content (`**bold**`, `## heading`, `- item`) is converted or stripped before rendering.
- Document ids are validated, which also blocks path traversal on the download route.

## Project layout

```
app/
  main.py          FastAPI routes and error mapping
  agent.py         orchestrator: analyze -> plan -> execute -> reflect -> compile
  tools.py         tool registry
  llm.py           OpenAI-compatible client (Groq / Ollama) with structured output + retry
  prompts.py       all prompts
  schemas.py       Pydantic models for API and LLM outputs
  docx_builder.py  Word rendering
  config.py        env-based settings
scripts/run_demo.py  runs both test cases end to end
tests/               offline tests with a fake LLM
```

## Scaling notes

- Sections are already drafted concurrently; `MAX_PARALLEL_TASKS` trades speed against provider rate limits.
- For production: move `/agent` to a job queue (returns a job id, poll for status), store documents in object storage, and add per-user conversation memory to refine a document across requests.
