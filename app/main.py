"""FastAPI entry point. Routes stay thin: validation in, agent call, errors mapped to HTTP."""
from __future__ import annotations

import logging
import re
from functools import lru_cache

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse

from .agent import AgentFailed, DocumentAgent, RequestRejected
from .config import settings
from .llm import LLMError, OpenAICompatibleLLM
from .schemas import AgentRequest, AgentResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(
    title="Autonomous Document Agent",
    description="Turns a natural-language request into a planned, executed, self-checked Word document.",
    version="1.0.0",
)

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_ID = re.compile(r"^[a-f0-9]{10}$")


@lru_cache
def get_agent() -> DocumentAgent:
    llm = OpenAICompatibleLLM(settings.base_url, settings.api_key, settings.model,
                              settings.temperature, settings.json_retries,
                              settings.reasoning_effort or None)
    return DocumentAgent(llm, settings.output_dir, settings.max_revisions,
                         settings.max_parallel_tasks)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "provider": settings.provider, "model": settings.model}


# Sync def: FastAPI runs it in a threadpool, so the blocking LLM calls don't stall the event loop.
@app.post("/agent", response_model=AgentResponse)
def run_agent(body: AgentRequest, agent: DocumentAgent = Depends(get_agent)) -> AgentResponse:
    try:
        return agent.run(body.request)
    except RequestRejected as exc:
        raise HTTPException(status_code=422, detail=f"Request rejected: {exc}")
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=f"LLM failure: {exc}")
    except AgentFailed as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/documents/{request_id}")
def download(request_id: str) -> FileResponse:
    if not _ID.match(request_id):  # also blocks path traversal
        raise HTTPException(status_code=400, detail="Invalid document id")
    path = settings.output_dir / f"{request_id}.docx"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Document not found")
    return FileResponse(path, media_type=DOCX_MIME, filename=f"{request_id}.docx")
