"""
Supervisor Agent: orchestration only.

The Supervisor decides *what happens next*. It does not research or write;
that's the Analysis and Generation agents' job. Its decisions are plain Python
so students can read exactly why the workflow went where it did:

    understand_request()  is this a blog request, and what is the topic?
    understand_follow_up()  while a draft waits for review: new request, or feedback on it?
    create_plan()         the steps this workflow will take
    progress()            mark plan steps done / active as work moves on
    decide_next()         route to analysis, generation, publish, or end

The supervisor nodes (nodes/supervisor_nodes.py) call these and report them
in the timeline; graph/workflow.py uses decide_next() for conditional routing.
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Literal

from graph.state import ContentCrewState, PlanStep

NextStep = Literal["analysis", "generation", "publish", "end"]

PLAN_TEMPLATE: list[PlanStep] = [
    {"id": "understand", "label": "Understand the request and company context", "status": "pending"},
    {"id": "research", "label": "Research competitor content", "status": "pending"},
    {"id": "analyze", "label": "Analyze keywords and content gaps", "status": "pending"},
    {"id": "approve_research", "label": "Get your approval on the research", "status": "pending"},
    {"id": "write", "label": "Write the blog post", "status": "pending"},
    {"id": "approve_content", "label": "Get your approval on the draft", "status": "pending"},
    {"id": "publish", "label": "Publish", "status": "pending"},
]

# "write me a blog about X", "draft an article on X", "blog post about X" ...
_LEAD_IN = re.compile(
    r"^\s*(?:please\s+|can you\s+|could you\s+)*"
    r"(?:write|create|draft|make|produce|generate)?\s*(?:me|us)?\s*(?:a|an)?\s*"
    r"(?:short\s+|long\s+|detailed\s+|new\s+)?"
    r"(?:blog\s*post|blog|post|article|piece)\s+(?:about|on|for|covering|explaining)\s+",
    re.IGNORECASE,
)
_VERB_ONLY = re.compile(r"^\s*(?:please\s+)?(?:write|create|draft|make|produce)\s+(?:me\s+|us\s+)?(?:a\s+|an\s+)?", re.IGNORECASE)
_TRAILING = re.compile(r"\s+for\s+(?:our|my|the)\s+(?:target\s+)?(?:audience|readers|customers)\s*$", re.IGNORECASE)
_WRITING_WORDS = re.compile(r"\b(write|draft|blog|post|article|content|piece)\b", re.IGNORECASE)

CANCELLED_REPLY = "Request cancelled. Nothing was written or published. Send a new request whenever you're ready."

UNSUPPORTED_REPLY = (
    "I coordinate research-backed blog posts. Tell me what to write about, for example: "
    "Write me a blog about Agentic AI."
)

ROUTE_MESSAGES = {
    "analysis": "Assigned research to the Analysis Agent",
    "generation": "Research approved. Assigned writing to the Generation Agent",
    "publish": "Draft approved. Publishing the post",
}


class Supervisor:
    name = "supervisor"

    @staticmethod
    def understand_request(message: str) -> tuple[Literal["blog", "unsupported"], str]:
        """Classify the request and pull out the topic."""
        text = message.strip()
        if not _WRITING_WORDS.search(text):
            return "unsupported", ""
        topic = _LEAD_IN.sub("", text, count=1)
        if topic == text:
            topic = _VERB_ONLY.sub("", text, count=1)
        topic = _TRAILING.sub("", topic).strip(" .!?\"'")
        if not topic or len(topic) < 2:
            return "unsupported", ""
        return "blog", topic[:120]

    @staticmethod
    def understand_follow_up(message: str) -> Literal["new_request", "feedback"]:
        """
        A message sent while a draft waits for review: a new post ("Write a blog about X"),
        or a change to this draft ("Make the introduction more concise")?
        Only an explicit "write/draft a post about ..." counts as a new request.
        """
        return "new_request" if _LEAD_IN.match(message.strip()) else "feedback"

    @staticmethod
    def create_plan() -> list[PlanStep]:
        return deepcopy(PLAN_TEMPLATE)

    @staticmethod
    def progress(plan: list[PlanStep], active_id: str | None) -> list[PlanStep]:
        """Everything before `active_id` is done, it is active, the rest pending. None = all done."""
        updated = deepcopy(plan)
        ids = [step["id"] for step in updated]
        active_index = ids.index(active_id) if active_id in ids else len(ids)
        for i, step in enumerate(updated):
            step["status"] = "done" if i < active_index else "active" if i == active_index else "pending"
        return updated

    @staticmethod
    def cancel(plan: list[PlanStep]) -> list[PlanStep]:
        """After "Cancel request": finished steps stay done, everything else is skipped."""
        updated = deepcopy(plan)
        for step in updated:
            if step["status"] != "done":
                step["status"] = "skipped"
        return updated

    @staticmethod
    def decide_next(state: ContentCrewState) -> NextStep:
        """The routing rule. Checked by `route_task` every time work comes back to the Supervisor."""
        if state.get("request_type") != "blog" or state.get("cancelled"):
            return "end"
        if state.get("publish_status") == "published":
            return "end"
        if state.get("content_approved"):
            return "publish"
        if state.get("research_approved"):
            return "generation"
        return "analysis"

    @staticmethod
    def plan_step_for(next_step: NextStep) -> str | None:
        return {"analysis": "research", "generation": "write", "publish": "publish"}.get(next_step)
