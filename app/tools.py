"""Tool registry.

The planner picks tools by name; the executor dispatches through TOOL_REGISTRY.
Adding a capability = write a function with the `Tool` signature and register it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

from . import prompts
from .llm import LLM
from .schemas import Block, Interpretation, Section, Task


@dataclass
class AgentContext:
    request: str
    interpretation: Interpretation
    sections: dict[int, Section] = field(default_factory=dict)  # task id -> drafted section

    def brief(self) -> str:
        i = self.interpretation
        return json.dumps({
            "original_request": self.request,
            "doc_type": i.doc_type, "title": i.title, "audience": i.audience,
            "objective": i.objective, "key_facts": i.key_facts,
            "conflicts": i.conflicts, "assumptions": i.assumptions,
        }, indent=2)


Tool = Callable[[Task, AgentContext, LLM, "str | None"], Section]


def _section_prompt(task: Task, ctx: AgentContext, feedback: str | None) -> str:
    prompt = (f"DOCUMENT BRIEF:\n{ctx.brief()}\n\n"
              f"SECTION TO WRITE: {task.section_title}\n"
              f"WHAT IT MUST COVER: {task.instructions or task.description}")
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
    "executive_summary": executive_summary,
    "assumptions_register": assumptions_register,
}

# Tools that need the rest of the document to exist first.
DEFERRED_TOOLS = {"executive_summary"}
