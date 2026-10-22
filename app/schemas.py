"""Pydantic models shared across the API, the agent and the LLM layer.

Every LLM output is parsed into one of these models, so malformed or
hallucinated structures (e.g. a tool name that doesn't exist) fail fast
instead of crashing later in the pipeline.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .reference_data import RATE_CARD

# ---------------------------------------------------------------- API layer


class AgentRequest(BaseModel):
    request: str = Field(..., min_length=10, max_length=4000,
                         description="Natural-language description of the document you need.")

    @field_validator("request")
    @classmethod
    def strip_and_check(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 10:
            raise ValueError("request is too short to act on")
        return v


# ----------------------------------------------------- step 1: understanding


class Interpretation(BaseModel):
    is_document_request: bool = True
    rejection_reason: str | None = None
    doc_type: str = "business_document"
    title: str = "Untitled Document"
    audience: str = "General business stakeholders"
    objective: str = ""
    key_facts: list[str] = []
    missing_info: list[str] = []
    conflicts: list[str] = []
    assumptions: list[str] = []


# ---------------------------------------------------------- step 2: planning

ToolName = Literal["draft_section", "build_table", "estimate_costs",
                   "executive_summary", "assumptions_register"]
TaskStatus = Literal["pending", "done", "failed"]


class Task(BaseModel):
    id: int
    description: str
    tool: ToolName
    section_title: str
    instructions: str = ""
    status: TaskStatus = "pending"
    error: str | None = None


class Plan(BaseModel):
    reasoning: str = ""
    # Facts every section must agree on (schedule, approach, roles), decided once before
    # sections are drafted in parallel, so independently written sections can't drift apart.
    key_decisions: list[str] = []
    tasks: list[Task] = Field(..., min_length=2, max_length=12)


# ------------------------------------------------- step 3: document content


class Block(BaseModel):
    type: Literal["paragraph", "bullets", "numbered", "table"]
    text: str | None = None
    items: list[str] | None = None
    headers: list[str] | None = None
    rows: list[list[str]] | None = None

    # Models often emit numbers in tables (week 1, cost 2500). Pydantic v2 won't
    # coerce int -> str, so normalise here instead of failing the whole section.
    @field_validator("text", mode="before")
    @classmethod
    def _text_to_str(cls, v):
        return v if v is None or isinstance(v, str) else str(v)

    @field_validator("items", "headers", mode="before")
    @classmethod
    def _list_to_str(cls, v):
        return [("" if x is None else str(x)) for x in v] if isinstance(v, list) else v

    @field_validator("rows", mode="before")
    @classmethod
    def _rows_to_str(cls, v):
        if not isinstance(v, list):
            return v
        return [[("" if c is None else str(c)) for c in row] if isinstance(row, list) else row
                for row in v]


class Section(BaseModel):
    title: str
    blocks: list[Block] = Field(..., min_length=1)


# ------------------------------------------------- estimate_costs tool I/O


class CostLineItem(BaseModel):
    role: str
    activity: str = ""
    days: float = Field(..., gt=0, le=500)

    @field_validator("role")
    @classmethod
    def role_on_rate_card(cls, v: str) -> str:
        # Rejecting unknown roles here sends the error back to the model via the retry
        # loop, so it can only pick rates that exist; it can never invent a price.
        role = RATE_CARD.canonical_role(v)
        if role is None:
            raise ValueError(f"unknown role '{v}'; use one of: {', '.join(RATE_CARD.day_rates)}")
        return role


class CostEstimate(BaseModel):
    line_items: list[CostLineItem] = Field(..., min_length=2, max_length=12)
    notes: str = ""


# ------------------------------------------------------- step 4: reflection


class ReviewIssue(BaseModel):
    section_title: str
    problem: str
    fix_instruction: str


class Review(BaseModel):
    passed: bool
    score: int = Field(..., ge=1, le=10)
    issues: list[ReviewIssue] = []


class ReflectionResult(BaseModel):
    initial_score: int
    initial_passed: bool
    issues_found: list[ReviewIssue] = []
    sections_revised: list[str] = []
    final_score: int | None = None
    final_passed: bool | None = None


# ----------------------------------------------------------- API response


class UsageStats(BaseModel):
    llm_calls: int = 0          # logical generate() calls made by the agent
    api_requests: int = 0       # HTTP requests incl. JSON-repair retries
    prompt_tokens: int = 0
    completion_tokens: int = 0


class StepLog(BaseModel):
    step: str
    detail: str
    duration_ms: int


class AgentResponse(BaseModel):
    request_id: str
    status: Literal["completed", "completed_with_warnings"]
    interpretation: Interpretation
    plan: list[Task]
    reflection: ReflectionResult
    summary: str
    document_url: str
    execution_log: list[StepLog]
    usage: UsageStats
    models: dict[str, str] = {}
    total_duration_ms: int
