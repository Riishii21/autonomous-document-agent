"""The autonomous document agent.

Pipeline:  analyze -> plan -> execute (tool calls) -> reflect & revise -> compile .docx

The LLM decides WHAT to do (document type, assumptions, TODO list, content, review verdict).
Plain Python decides HOW it runs (tool dispatch, ordering, failure handling, rendering),
so the parts that must never fail creatively stay deterministic.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from . import prompts
from .docx_builder import build_docx
from .llm import LLM, Meter, MeteredLLM
from .reference_data import RATE_CARD
from .schemas import (AgentResponse, Interpretation, Plan, ReflectionResult, Review,
                      Section, StepLog, Task)
from .tools import DEFERRED_TOOLS, TOOL_REGISTRY, AgentContext

log = logging.getLogger("agent")


class RequestRejected(ValueError):
    """The request can't be turned into a business document (guardrail)."""


class AgentFailed(RuntimeError):
    """Execution could not produce a usable document."""


class DocumentAgent:
    def __init__(self, llm: LLM, output_dir: Path, max_revisions: int = 1, max_parallel: int = 3,
                 reviewer_llm: LLM | None = None, model_names: dict[str, str] | None = None):
        self.llm = llm
        # An independent reviewer model reduces "grading your own homework" bias.
        self.reviewer_llm = reviewer_llm or llm
        self.output_dir = output_dir
        self.max_revisions = max_revisions
        self.max_parallel = max(1, max_parallel)
        self.model_names = model_names or {}

    # ------------------------------------------------------------------ public

    def run(self, request: str) -> AgentResponse:
        request_id = uuid.uuid4().hex[:10]
        steps: list[StepLog] = []
        started = time.perf_counter()
        # This agent instance is shared across concurrent requests, so everything
        # run-specific (usage metering included) lives in local objects, never on self.
        meter = Meter()
        llm = MeteredLLM(self.llm, meter)
        reviewer = MeteredLLM(self.reviewer_llm, meter)

        @contextmanager
        def step(name: str):
            t0 = time.perf_counter()
            entry = {"detail": ""}
            yield entry
            ms = int((time.perf_counter() - t0) * 1000)
            steps.append(StepLog(step=name, detail=entry["detail"], duration_ms=ms))
            log.info("[%s] %s (%d ms) %s", request_id, name, ms, entry["detail"])

        with step("analyze") as s:
            interp = self._analyze(request, llm)
            s["detail"] = (f"doc_type={interp.doc_type}; {len(interp.missing_info)} missing, "
                           f"{len(interp.conflicts)} conflicts, {len(interp.assumptions)} assumptions")

        with step("plan") as s:
            plan = self._plan(request, interp, llm)
            s["detail"] = f"{len(plan.tasks)} tasks: " + ", ".join(t.tool for t in plan.tasks)

        ctx = AgentContext(request=request, interpretation=interp,
                           key_decisions=plan.key_decisions, outline=plan.tasks)
        with step("execute") as s:
            recovered = self._execute(plan.tasks, ctx, llm)
            done = sum(t.status == "done" for t in plan.tasks)
            s["detail"] = (f"{done}/{len(plan.tasks)} tasks completed"
                           + (f"; recovered on retry: {recovered}" if recovered else ""))

        with step("reflect") as s:
            reflection = self._reflect_and_revise(plan.tasks, ctx, llm, reviewer)
            s["detail"] = (f"score {reflection.initial_score}"
                           + (f" -> {reflection.final_score}" if reflection.final_score else "")
                           + f"; revised: {reflection.sections_revised or 'none'}")

        with step("compile_docx") as s:
            sections = self._ordered_sections(plan.tasks, ctx)
            path = build_docx(self.output_dir / f"{request_id}.docx", interp, sections, request_id)
            s["detail"] = f"{len(sections)} sections -> {path.name}"

        failed = [t for t in plan.tasks if t.status == "failed"]
        passed = reflection.final_passed if reflection.final_passed is not None else reflection.initial_passed
        response = AgentResponse(
            request_id=request_id,
            status="completed" if not failed and passed else "completed_with_warnings",
            interpretation=interp,
            plan=plan.tasks,
            reflection=reflection,
            summary=self._summary(interp, plan.tasks, reflection),
            document_url=f"/documents/{request_id}",
            execution_log=steps,
            usage=meter.usage,
            models=self.model_names,
            total_duration_ms=int((time.perf_counter() - started) * 1000),
        )
        # Persist the full trace next to the document so any run can be audited later.
        (self.output_dir / f"{request_id}.json").write_text(response.model_dump_json(indent=2))
        return response

    # ------------------------------------------------------------------ steps

    def _analyze(self, request: str, llm: LLM) -> Interpretation:
        interp = llm.generate(prompts.ANALYZE_SYSTEM, f"USER REQUEST:\n{request}", Interpretation)
        if not interp.is_document_request:
            raise RequestRejected(interp.rejection_reason or "Request is not a business-document request.")
        return interp

    def _plan(self, request: str, interp: Interpretation, llm: LLM) -> Plan:
        user = (f"USER REQUEST:\n{request}\n\nANALYSIS:\n{interp.model_dump_json(indent=2)}"
                f"\n\nAVAILABLE ROLES: {', '.join(RATE_CARD.day_rates)}")
        plan = llm.generate(prompts.PLAN_SYSTEM, user, Plan)

        # Deterministic safety net on top of the LLM's plan.
        for n, task in enumerate(plan.tasks, 1):
            task.id, task.status, task.error = n, "pending", None
        needs_register = interp.assumptions or interp.missing_info or interp.conflicts
        if needs_register and not any(t.tool == "assumptions_register" for t in plan.tasks):
            plan.tasks.append(Task(id=len(plan.tasks) + 1, tool="assumptions_register",
                                   section_title="Assumptions and Open Questions",
                                   description="Record assumptions, conflicts and open questions (added by guard)"))
        return plan

    def _execute(self, tasks: list[Task], ctx: AgentContext, llm: LLM) -> list[str]:
        """Run content tasks concurrently, retry failures once, then run deferred tasks.

        Content sections only depend on the shared brief, not on each other, so they can be
        drafted in parallel. Deferred tools (the executive summary) need the finished content.
        Returns the titles of sections that succeeded only on the recovery retry."""
        content = [t for t in tasks if t.tool not in DEFERRED_TOOLS]
        deferred = [t for t in tasks if t.tool in DEFERRED_TOOLS]

        with ThreadPoolExecutor(max_workers=self.max_parallel) as pool:
            list(pool.map(lambda t: self._run_task(t, ctx, llm), content))

        # Recovery pass: a failure is often transient (rate limit, one bad generation).
        recovered = []
        for task in [t for t in content if t.status == "failed"]:
            log.info("Retrying failed task %s (%s)", task.id, task.section_title)
            if self._run_task(task, ctx, llm):
                recovered.append(task.section_title)

        if not any(t.status == "done" for t in content):
            raise AgentFailed("No section could be generated; see task errors.")

        for task in deferred:
            self._run_task(task, ctx, llm)
        return recovered

    def _run_task(self, task: Task, ctx: AgentContext, llm: LLM, feedback: str | None = None) -> bool:
        tool = TOOL_REGISTRY[task.tool]
        try:
            ctx.sections[task.id] = tool(task, ctx, llm, feedback)
            task.status, task.error = "done", None
            return True
        except Exception as exc:  # one bad section must not sink the whole document
            log.exception("Task %s (%s) failed", task.id, task.tool)
            if task.id not in ctx.sections:  # keep the previous version if a revision fails
                task.status = "failed"
            task.error = f"{type(exc).__name__}: {exc}"[:300]
            return False

    def _reflect_and_revise(self, tasks: list[Task], ctx: AgentContext,
                            llm: LLM, reviewer: LLM) -> ReflectionResult:
        review = self._review(tasks, ctx, reviewer)
        result = ReflectionResult(initial_score=review.score, initial_passed=review.passed,
                                  issues_found=review.issues)

        for _ in range(self.max_revisions):
            if review.passed or not review.issues:
                break
            revised = self._apply_review(review, tasks, ctx, llm)
            if not revised:
                break
            result.sections_revised += revised
            review = self._review(tasks, ctx, reviewer)
            result.final_score, result.final_passed = review.score, review.passed
        return result

    def _review(self, tasks: list[Task], ctx: AgentContext, reviewer: LLM) -> Review:
        doc = [s.model_dump() for s in self._ordered_sections(tasks, ctx)]
        failed = [t.section_title for t in tasks if t.status == "failed"]
        user = (f"ORIGINAL REQUEST:\n{ctx.request}\n\n"
                f"ANALYSIS:\n{ctx.interpretation.model_dump_json(indent=2)}\n\n"
                f"KEY DECISIONS (sections must agree with these): {ctx.key_decisions}\n\n"
                f"DRAFTED DOCUMENT:\n{json.dumps(doc)[:16000]}")
        if failed:  # tell the reviewer what is absent so it can request those sections again
            user += f"\n\nPLANNED SECTIONS THAT FAILED TO GENERATE (currently missing): {failed}"
        return reviewer.generate(prompts.REVIEW_SYSTEM, user, Review)

    def _apply_review(self, review: Review, tasks: list[Task], ctx: AgentContext, llm: LLM) -> list[str]:
        # Group feedback per section so each section is rewritten at most once.
        feedback: dict[str, list[str]] = {}
        for issue in review.issues:
            feedback.setdefault(issue.section_title.strip(), []).append(
                f"{issue.problem} -> {issue.fix_instruction}")

        revised: list[str] = []
        for title, notes in feedback.items():
            task = self._match_task(title, tasks)
            if task is None:  # reviewer found a missing section: plan a new task for it
                task = Task(id=max(t.id for t in tasks) + 1, tool="draft_section", section_title=title,
                            description=f"Add missing section '{title}' (requested by self-check)",
                            instructions=" ".join(notes))
                self._insert_before_closing(task, tasks)
                ok = self._run_task(task, ctx, llm)
            elif task.tool == "assumptions_register":
                continue  # deterministic section; it mirrors the analysis exactly
            else:
                ok = self._run_task(task, ctx, llm, feedback=" | ".join(notes))
            if ok:
                revised.append(task.section_title)

        # Keep the executive summary consistent with revised content.
        summary = next((t for t in tasks if t.tool == "executive_summary"), None)
        if revised and summary and summary.section_title not in revised:
            if self._run_task(summary, ctx, llm):
                revised.append(summary.section_title)
        return revised

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _match_task(title: str, tasks: list[Task]) -> Task | None:
        """Exact (case-insensitive) title first; substring only if exactly one task matches,
        so 'Risks' can't silently attach to the wrong section when titles overlap."""
        t = title.strip().lower()
        exact = [task for task in tasks if task.section_title.strip().lower() == t]
        if exact:
            return exact[0]
        partial = [task for task in tasks
                   if t in task.section_title.lower() or task.section_title.lower() in t]
        return partial[0] if len(partial) == 1 else None

    @staticmethod
    def _insert_before_closing(new: Task, tasks: list[Task]) -> None:
        idx = next((i for i, t in enumerate(tasks) if t.tool == "assumptions_register"), len(tasks))
        tasks.insert(idx, new)

    @staticmethod
    def _ordered_sections(tasks: list[Task], ctx: AgentContext) -> list[Section]:
        return [ctx.sections[t.id] for t in tasks if t.id in ctx.sections]

    @staticmethod
    def _summary(interp: Interpretation, tasks: list[Task], r: ReflectionResult) -> str:
        done = sum(t.status == "done" for t in tasks)
        parts = [f"Generated a {interp.doc_type.replace('_', ' ')} titled '{interp.title}' "
                 f"by planning and executing {len(tasks)} tasks ({done} succeeded)."]
        if interp.assumptions:
            parts.append(f"Made {len(interp.assumptions)} documented assumption(s).")
        if interp.conflicts:
            parts.append(f"Identified and addressed {len(interp.conflicts)} conflicting requirement(s).")
        if r.sections_revised:
            parts.append(f"Self-check scored the first draft {r.initial_score}/10 and revised "
                         f"{len(r.sections_revised)} section(s); final score {r.final_score}/10.")
        else:
            parts.append(f"Self-check scored the draft {r.initial_score}/10; no revision needed.")
        return " ".join(parts)
