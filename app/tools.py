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
import logging
import re
from dataclasses import dataclass, field
from typing import Callable

from . import prompts
from .llm import LLM
from .reference_data import RATE_CARD
from .schemas import Block, CostEstimate, Interpretation, Section, Task

log = logging.getLogger("agent.tools")


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
    chooses roles/items (validated against the rate card) and quantities. Its free-text
    notes are also stripped of any figures, so model arithmetic can't leak back in."""
    rc = RATE_CARD
    prompt = (_section_prompt(task, ctx, feedback)
              + f"\n\nAVAILABLE ROLES: {', '.join(rc.day_rates)}"
              + f"\nAVAILABLE NON-LABOUR ITEMS: {', '.join(rc.non_labour_items)}")
    est = llm.generate(prompts.COST_SYSTEM, prompt, CostEstimate)

    rows, labour, total_days = [], 0.0, 0.0
    for item in est.line_items:
        rate = rc.day_rates[item.role]
        labour += rate * item.days
        total_days += item.days
        rows.append([item.role, item.activity, f"{item.days:g} days", rc.money(rate),
                     rc.money(rate * item.days)])
    other = 0.0
    for o in est.other_items:
        price = rc.non_labour_items[o.item]
        other += price * o.quantity
        rows.append([o.item, "Non-labour", f"x {o.quantity:g}", rc.money(price),
                     rc.money(price * o.quantity)])

    subtotal = labour + other
    contingency = subtotal * rc.contingency_pct / 100
    total = subtotal + contingency
    rows += [
        ["Subtotal", "", f"{total_days:g} days", "", rc.money(subtotal)],
        [f"Contingency ({rc.contingency_pct:g}%)", "", "", "", rc.money(contingency)],
        ["**Estimated total**", "", "", "", f"**{rc.money(total)}**"],
    ]
    notes = _strip_figures(est.notes)
    blocks = [
        Block(type="paragraph", text=(
            f"Estimated delivery cost: {rc.money(total)} (labour {rc.money(labour)} for "
            f"{total_days:g} person-days, non-labour {rc.money(other)}, plus "
            f"{rc.contingency_pct:g}% contingency).")),
        Block(type="table", headers=["Item", "Activity", "Quantity", "Unit rate", "Cost"], rows=rows),
        Block(type="paragraph", text=(
            "Effort figures are estimates; rates come from the reference rate card (illustrative "
            "data) and all totals are calculated, not generated."
            + (f" Basis: {notes}" if notes else ""))),
    ]
    return Section(title=task.section_title, blocks=blocks)


_MONEY = re.compile(r"(?:[$\u20ac\u00a3\u20b9]|\b(?:USD|INR|EUR|GBP)\b)\s?\d|\d\s?%|\b\d[\d,.]*\s?[kK]\b")


def _strip_figures(notes: str) -> str:
    """Drop any sentence in the model's notes that states money, percentages or totals.
    The computed table is the single source of truth for numbers."""
    sentences = re.split(r"(?<=[.!?])\s+", notes.strip())
    return " ".join(s for s in sentences if s and not _MONEY.search(s))


MAX_DOC_CHARS = 60_000  # ~15k tokens; far above a normal business document


def render_sections_text(sections: list[Section]) -> str:
    """Compact plain-text view of the document for LLM consumption (reviewer, summary).

    JSON costs ~2x the characters of the same content as text. If the limit is ever hit,
    the cut is marked explicitly so the model knows it isn't seeing everything."""
    out: list[str] = []
    for s in sections:
        out.append(f"## {s.title}")
        for b in s.blocks:
            if b.type == "paragraph" and b.text:
                out.append(b.text)
            elif b.type in ("bullets", "numbered") and b.items:
                out += [f"- {i}" for i in b.items]
            elif b.type == "table" and b.headers:
                out.append(" | ".join(b.headers))
                out += [" | ".join(c.replace("\n", "; ") for c in r) for r in (b.rows or [])]
        out.append("")
    text = "\n".join(out)
    if len(text) > MAX_DOC_CHARS:
        log.warning("Document text truncated for LLM input (%d chars)", len(text))
        text = text[:MAX_DOC_CHARS] + "\n[... DOCUMENT TRUNCATED: remaining content not shown ...]"
    return text


def executive_summary(task: Task, ctx: AgentContext, llm: LLM, feedback: str | None = None) -> Section:
    drafted = [s for tid, s in sorted(ctx.sections.items()) if tid != task.id]
    prompt = (f"DOCUMENT BRIEF:\n{ctx.brief()}\n\nDRAFTED SECTIONS:\n{render_sections_text(drafted)}"
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
