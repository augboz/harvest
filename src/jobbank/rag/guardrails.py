"""The agent's guardrails, as pure functions.

No ADK import here: the agent's callbacks (agent/jobbank_agent/agent.py) are
thin adapters over these, so every rule is unit-tested offline and the same
rules can be reused by any other agent framework.

Three guardrails:

1. Least privilege. Only read tools, the profile pinned, result sizes capped.
   Defence in depth: the MCP server in READONLY mode does not even REGISTER
   the spending tools, and the toolset's tool_filter hides them too; this is
   the third layer, at the moment of the call.

2. Cite or strip. The agent cites records as [id]. Every cited id must have
   been returned by a tool during THIS answer; anything else is a fabricated
   or stale citation and is removed. Validity is measured BEFORE stripping and
   reported, so the evaluation can say how often the model cited something no
   tool returned (the guardrail's own guarantee would otherwise read as 100%).

3. Low-confidence refusal. If retrieval's best match is weak (top vector
   cosine below a threshold) and the answer cites nothing verifiable, the
   answer is replaced with a plain "no supporting records". The threshold must
   be CALIBRATED on the evaluation's unanswerable queries before it means
   anything: RRF scores are rank-based and cannot drive this, and cosine
   ranges are model-specific. Until calibrated it defaults to 0.0 (never
   refuses on confidence alone) and says so.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

READ_TOOLS = ("semantic_search", "search_jobs", "status", "run_history")
PINNED_PROFILE = "default"
MAX_K = 20
CITE_RE = re.compile(r"\[([0-9a-f]{12})\]")
REFUSAL = ("I couldn't find records that support an answer to that. The closest "
           "matches were too weak to rely on, so I'd rather not guess.")
UNCITED_NOTE = ("\n\n(Note: no record was cited for this answer, so treat it as "
                "unverified.)")


# ------------------------------------------------------- 1. least privilege --

def check_tool_call(name: str, args: dict) -> tuple[bool, dict, str]:
    """(allowed, args to use, reason). Never raises: a refusal is returned as a
    tool result so the model can recover and say what it could not do."""
    if name not in READ_TOOLS:
        return False, args, f"tool {name!r} is not available to this agent"
    safe = dict(args)
    safe["profile"] = PINNED_PROFILE
    for key in ("k", "limit"):
        if key in safe:
            try:
                safe[key] = max(1, min(int(safe[key]), MAX_K))
            except (TypeError, ValueError):
                safe[key] = 10
    return True, safe, ""


# ------------------------------------------------------- 2. cite or strip ---

def ids_from_tool_response(name: str, response) -> tuple[set[str], float | None]:
    """Record ids a tool returned, and (for semantic_search) its top cosine."""
    if not isinstance(response, dict):
        return set(), None
    # MCP tool results may arrive wrapped; accept the payload either way
    payload = response.get("structuredContent") or response.get("result") or response
    if not isinstance(payload, dict):
        return set(), None
    rows = payload.get("hits") if name == "semantic_search" else payload.get("jobs")
    ids = {str(r.get("id")) for r in (rows or []) if isinstance(r, dict) and r.get("id")}
    top = payload.get("top_cosine") if name == "semantic_search" else None
    try:
        top = float(top) if top is not None else None
    except (TypeError, ValueError):
        top = None
    return ids, top


@dataclass
class CitationCheck:
    text: str                       # the answer after stripping
    valid: list[str] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)

    @property
    def cited_any_valid(self) -> bool:
        return bool(self.valid)


def check_citations(answer: str, known_ids: set[str]) -> CitationCheck:
    cited = CITE_RE.findall(answer)
    valid = [c for c in dict.fromkeys(cited) if c in known_ids]
    invalid = [c for c in dict.fromkeys(cited) if c not in known_ids]
    text = answer
    for bad in invalid:
        text = text.replace(f"[{bad}]", "")
    text = re.sub(r"[ \t]{2,}", " ", text)
    return CitationCheck(text=text, valid=valid, invalid=invalid)


# ------------------------------------------------- 3. low-confidence refusal --

@dataclass
class Verdict:
    text: str
    refused: bool
    downgraded: bool
    citations: CitationCheck


def finalise(answer: str, known_ids: set[str], top_cosines: list[float],
             threshold: float) -> Verdict:
    """Apply guardrails 2 and 3 to a final answer.

    - Strip citations no tool returned.
    - If retrieval ran and its best match was below the threshold, and nothing
      verifiable is cited, refuse.
    - If tools returned records but none is cited, keep the answer but flag it
      as unverified (a status question, say, legitimately cites no record).
    """
    cc = check_citations(answer, known_ids)
    best = max(top_cosines) if top_cosines else None
    if best is not None and best < threshold and not cc.cited_any_valid:
        return Verdict(text=REFUSAL, refused=True, downgraded=False, citations=cc)
    if known_ids and not cc.cited_any_valid:
        return Verdict(text=cc.text + UNCITED_NOTE, refused=False, downgraded=True,
                       citations=cc)
    return Verdict(text=cc.text, refused=False, downgraded=False, citations=cc)


def calibrate_threshold(answerable: list[float], unanswerable: list[float]) -> dict:
    """The cosine cut-off that best separates queries the records CAN answer
    from ones they cannot (both measured as retrieval's top cosine), chosen to
    maximise balanced accuracy. Reports the error rates it implies, which are
    what the README should quote, not the threshold alone."""
    if not answerable or not unanswerable:
        raise ValueError("need top cosines for both answerable and unanswerable queries")
    candidates = sorted(set(answerable) | set(unanswerable))
    best = None
    for t in candidates + [max(candidates) + 1e-6]:
        false_refusal = sum(1 for c in answerable if c < t) / len(answerable)
        false_answer = sum(1 for c in unanswerable if c >= t) / len(unanswerable)
        bal = 1 - (false_refusal + false_answer) / 2
        if best is None or bal > best["balanced_accuracy"]:
            best = {"threshold": round(t, 4), "balanced_accuracy": round(bal, 4),
                    "false_refusal_rate": round(false_refusal, 4),
                    "false_answer_rate": round(false_answer, 4)}
    return best
