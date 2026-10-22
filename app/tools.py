"""Tool registry.

The planner picks tools by name; the executor dispatches through TOOL_REGISTRY.
Adding a capability = write a function with the `Tool` signature and register it.

Tools differ in how much they trust the model:
  draft_section / build_table / executive_summary  -> LLM writes the content
  estimate_costs                                   -> LLM estimates, code computes (hybrid)
  assumptions_register                             -> no LLM at all (deterministic)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

from . import prompts
from .llm import LLM
from .reference_data import RATE_CARD
from .schemas import Block, CostEstimate, Interpretation, Section, Task


@dataclass
class AgentContext:
    request: str
    interpretation: Interpretation
    key_decisions: list[str] = field(default_factory=list)
    outline: list[Task] = field(default_factory=list)
    sections: dict[int, Section] = field(default_factory=dict)  # task id -> drafted section

    def brief(self) -> str:
        """Shared context given to every section writer. Because sections are drafted in
        parallel, this is the only way they 'see' each other, so it carries the decisions
        they must agree on and what each other section covers."""
        i = self.interpretation
        return json.dumps({
            "original_request": self.request,
            "doc_type": i.doc_type, "title": i.title, "audience": i.audience,
            "objective": i.objective, "key_facts": i.key_facts,
            "conflicts": i.conflicts, "assumptions": i.assumptions,
            "key_decisions_all_sections_must_follow": self.key_decisions,
            "document_outline": [f"{t.section_title}: {t.instructions or t.description}"
                                 for t in self.outline],
            "role_names_to_use_for_owners": list(RATE_CARD.day_rates),
        }, indent=2)


Tool = Callable[[Task, AgentContext, LLM, "str | None"], Section]


def _section_prompt(task: Task, ctx: AgentContext, feedback: str | None) -> str:
    prompt = (f"DOCUMENT BRIEF:\n{ctx.brief()}\n\n"
              f"SECTION TO WRITE: {task.section_title}\n"
              f"WHAT IT MUST COVER: {task.instructions or task.description}\n"
              "Stay consistent with key_decisions (dates, phases, approach, roles) and don't "
              "repeat material that another section in document_outline owns.")
    if feedback:
        prompt += f"\n\nREVIEWER FEEDBACK TO FIX IN THIS VERSION: {feedback}"
    return prompt


def draft_section(task: Task, ctx: AgentContext, llm: LLM, feedback: str | None = None) -> Section:
    section = llm.generate(prompts.SECTION_SYSTEM, _section_prompt(task, ctx, feedback), Section)
    section.title = task.section_title
    return section


def build_table(task: Task, ctx: AgentContext, llm: LLM, feedback: str | None = None) -> Section:
    section = llm.generate(prompts.TABLE_SYSTEM, _section_prompt(task, ctx, feedback), Section)
    section.title = task.section_title
    for block in section.blocks:  # normalise ragged rows so the docx table never breaks
        if block.type == "table" and block.headers and block.rows:
            width = len(block.headers)
            block.rows = [(row + [""] * width)[:width] for row in block.rows]
    return section


def estimate_costs(task: Task, ctx: AgentContext, llm: LLM, feedback: str | None = None) -> Section:
    """Hybrid tool: the LLM estimates effort, deterministic code does the pricing and maths.

    LLMs are unreliable at arithmetic and will happily invent rates, so the model only
    chooses roles (validated against the rate card) and person-days."""
    rc = RATE_CARD
    prompt = (_section_prompt(task, ctx, feedback)
              + f"\n\nAVAILABLE ROLES: {', '.join(rc.day_rates)}")
    est = llm.generate(prompts.COST_SYSTEM, prompt, CostEstimate)

    rows, subtotal, total_days = [], 0.0, 0.0
    for item in est.line_items:
        rate = rc.day_rates[item.role]
        cost = rate * item.days
        subtotal += cost
        total_days += item.days
        rows.append([item.role, item.activity, f"{item.days:g}", rc.money(rate), rc.money(cost)])
    contingency = subtotal * rc.contingency_pct / 100
    rows += [
        ["Subtotal", "", f"{total_days:g}", "", rc.money(subtotal)],
        [f"Contingency ({rc.contingency_pct:g}%)", "", "", "", rc.money(contingency)],
        ["**Estimated total**", "", "", "", f"**{rc.money(subtotal + contingency)}**"],
    ]
    blocks = [
        Block(type="paragraph", text=(
            f"Estimated delivery cost: {rc.money(subtotal + contingency)} "
            f"({total_days:g} person-days, including {rc.contingency_pct:g}% contingency).")),
        Block(type="table", headers=["Role", "Activity", "Effort (days)", "Day rate", "Cost"], rows=rows),
        Block(type="paragraph", text=(
            "Effort figures are estimates; rates come from the reference rate card (illustrative "
            "data) and all totals are calculated, not generated."
            + (f" Basis: {est.notes}" if est.notes else ""))),
    ]
    return Section(title=task.section_title, blocks=blocks)


def executive_summary(task: Task, ctx: AgentContext, llm: LLM, feedback: str | None = None) -> Section:
    drafted = [s.model_dump() for tid, s in sorted(ctx.sections.items()) if tid != task.id]
    prompt = (f"DOCUMENT BRIEF:\n{ctx.brief()}\n\nDRAFTED SECTIONS:\n{json.dumps(drafted)[:12000]}"
              f"\n\nSECTION TO WRITE: {task.section_title}")
    if feedback:
        prompt += f"\n\nREVIEWER FEEDBACK TO FIX IN THIS VERSION: {feedback}"
    section = llm.generate(prompts.SUMMARY_SYSTEM, prompt, Section)
    section.title = task.section_title
    return section


def assumptions_register(task: Task, ctx: AgentContext, llm: LLM, feedback: str | None = None) -> Section:
    """Deterministic tool: no LLM call. It renders exactly what the analysis
    step recorded, so assumptions in the document can't drift or be hallucinated."""
    i = ctx.interpretation
    blocks = [Block(type="paragraph", text=(
        "This document was produced from an incomplete brief. The items below record "
        "what was assumed and what should be confirmed before the document is finalised."))]
    if i.assumptions:
        blocks.append(Block(type="table", headers=["#", "Assumption made"],
                            rows=[[str(n), a] for n, a in enumerate(i.assumptions, 1)]))
    if i.conflicts:
        blocks += [Block(type="paragraph", text="Conflicting requirements identified:"),
                   Block(type="bullets", items=i.conflicts)]
    if i.missing_info:
        blocks += [Block(type="paragraph", text="Open questions for the requester:"),
                   Block(type="numbered", items=[f"Please confirm: {m}" for m in i.missing_info])]
    if len(blocks) == 1:
        blocks.append(Block(type="paragraph", text="No material assumptions were required."))
    return Section(title=task.section_title, blocks=blocks)


TOOL_REGISTRY: dict[str, Tool] = {
    "draft_section": draft_section,
    "build_table": build_table,
    "estimate_costs": estimate_costs,
    "executive_summary": executive_summary,
    "assumptions_register": assumptions_register,
}

# Tools that need the rest of the document to exist first.
DEFERRED_TOOLS = {"executive_summary"}
