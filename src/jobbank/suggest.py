"""Propose hard-filter rules from accumulated feedback — for a human to approve.

Deliberately advisory. A rejection reason is one person's shorthand in the moment
("too far"), and turning that into a silent regex is how a pipeline quietly stops
surfacing whole categories of good roles. So this prints suggestions with the
evidence behind each one and never edits filters*.py.

Only reasons that recur are worth a rule; a one-off belongs in the scorer's
prompt context (see feedback.py), not in a hard exclusion.
"""
from __future__ import annotations

import json
import re

MIN_SUPPORT = 3   # a rule needs at least this many rejections behind it

PROMPT = """You are helping maintain the hard filters of a personal job-search pipeline.

Below are roles the candidate REMOVED from their shortlist, with the reason they
gave, and roles they PURSUED (applied to or progressed). Propose hard-filter rules
ONLY where a clear, repeated pattern justifies one.

A hard filter rejects a posting outright, before scoring. That is a blunt
instrument: propose a rule only when you would be comfortable never seeing that
kind of role again. Anything softer belongs in scoring, not filtering.

Rules to follow:
- Every rule needs at least {min_support} supporting rejections. Say how many.
- Never propose a rule that would have excluded any of the PURSUED roles. Check.
- Prefer title-based rules; description-based ones misfire far more often.
- If the evidence does not justify any rule, return an empty list. That is a fine
  and common answer — do not invent rules to seem useful.

REMOVED:
{removed}

PURSUED:
{pursued}

Return ONLY a JSON array, no prose:
[{{"rule": "<short name>", "pattern": "<python regex, case-insensitive>",
  "field": "title" | "description", "support": <int>,
  "evidence": "<the reasons that justify it>",
  "risk": "<what good roles this might wrongly exclude>"}}]"""


def _lines(rows: list[dict], signal: str, with_reason: bool) -> list[str]:
    out = []
    for r in rows:
        if str(r.get("Signal", "")).strip().lower() != signal:
            continue
        role = " ".join(str(r.get("Role", "")).split())
        company = " ".join(str(r.get("Company", "")).split())
        reason = " ".join(str(r.get("Reason", "")).split())
        if not role and not company:
            continue
        if with_reason:
            if not reason:
                continue
            out.append(f"- {role} at {company} — {reason}")
        else:
            out.append(f"- {role} at {company}")
    return out


def suggest_rules(rows: list[dict], settings) -> list[dict]:
    """Ask the LLM for candidate rules. Returns [] when there isn't the evidence."""
    removed = _lines(rows, "removed", with_reason=True)
    pursued = _lines(rows, "pursued", with_reason=False)
    if len(removed) < MIN_SUPPORT:
        print(f"  only {len(removed)} explained rejection(s); need at least "
              f"{MIN_SUPPORT} before any rule is worth proposing")
        return []

    api_key = settings.env("ANTHROPIC_API_KEY")
    if not api_key:
        print("  ANTHROPIC_API_KEY not set — cannot analyse feedback")
        return []

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=settings.cfg["scorer"]["model"],
        max_tokens=2000,
        messages=[{"role": "user", "content": PROMPT.format(
            min_support=MIN_SUPPORT,
            removed="\n".join(removed[-60:]),
            pursued="\n".join(pursued[-40:]) or "(none recorded yet)")}])
    text = resp.content[0].text
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        out = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    return [r for r in out if isinstance(r, dict) and r.get("pattern")]


def check_rule(pattern: str, rows: list[dict]) -> list[str]:
    """Pursued roles a proposed pattern would have wrongly excluded."""
    try:
        rx = re.compile(pattern, re.I)
    except re.error:
        return ["(invalid regex)"]
    hits = []
    for r in rows:
        if str(r.get("Signal", "")).strip().lower() != "pursued":
            continue
        role = str(r.get("Role", ""))
        if rx.search(role):
            hits.append(f"{role} at {r.get('Company', '')}")
    return hits


def report(rows: list[dict], settings) -> int:
    """Print suggestions with their evidence. Returns the number proposed."""
    rules = suggest_rules(rows, settings)
    if not rules:
        print("No filter rules suggested — not enough repeated signal yet.")
        return 0

    print(f"\n{len(rules)} suggested rule(s). Nothing has been applied — these are "
          f"for you to accept or ignore.\n")
    for i, r in enumerate(rules, 1):
        print(f"{i}. {r.get('rule', '(unnamed)')}  [{r.get('field', 'title')}, "
              f"support {r.get('support', '?')}]")
        print(f"   pattern : {r.get('pattern')}")
        print(f"   evidence: {r.get('evidence', '')}")
        if r.get("risk"):
            print(f"   risk    : {r['risk']}")
        # verify the model's own claim rather than trusting it
        clash = check_rule(r.get("pattern", ""), rows)
        if clash:
            print(f"   !! would ALSO have excluded roles you pursued: {'; '.join(clash[:3])}")
        print()
    print("To apply one, add it to the relevant check_* in src/jobbank/filters.py "
          "— deliberately a manual edit.")
    return len(rules)
