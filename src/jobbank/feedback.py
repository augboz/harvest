"""Turn the Feedback tab into a short block of prompt context.

The dashboard records why a role was removed, and which roles were pursued far
enough to change status. That is a handful of rows a week — plenty for a model
to read as context, nowhere near enough to fit weights against, so this stays
deliberately simple: summarise, cap, hand to the scorer.

It is context, never a rule. One "too far away" note must not quietly suppress
every non-local role; hard exclusions live in filters*.py where they are visible
and editable.
"""
from __future__ import annotations

from collections import Counter

# Enough signal to be useful, short enough that it can't crowd out the rubric or
# drift the scorer toward whatever was rejected most recently.
MAX_REASONS = 18
MAX_PURSUED = 10


def _clean(text: str) -> str:
    return " ".join(str(text or "").split())


def build_digest(rows: list[dict]) -> str:
    """A prompt fragment describing recent accept/reject signal, or ""."""
    removed, pursued = [], []
    for r in rows:
        signal = _clean(r.get("Signal")).lower()
        company = _clean(r.get("Company"))
        role = _clean(r.get("Role"))
        reason = _clean(r.get("Reason"))
        if not company and not role:
            continue
        if signal == "removed":
            # a removal with no reason still says "not this one", but it carries
            # no transferable lesson, so it is counted rather than quoted
            removed.append((company, role, reason))
        elif signal == "pursued":
            pursued.append((company, role))

    if not removed and not pursued:
        return ""

    parts = ["", "RECENT FEEDBACK FROM THE CANDIDATE (context, NOT hard rules):"]

    explained = [x for x in removed if x[2]]
    if explained:
        parts.append("Roles they removed, and why:")
        for company, role, reason in explained[-MAX_REASONS:]:
            parts.append(f'- {role} at {company}: "{reason}"')

    silent = len(removed) - len(explained)
    if silent:
        parts.append(f"({silent} further role(s) removed without a stated reason.)")

    if pursued:
        parts.append("Roles they actually applied to or progressed:")
        for company, role in pursued[-MAX_PURSUED:]:
            parts.append(f"- {role} at {company}")

    themes = Counter()
    for _c, _r, reason in explained:
        for token in ("location", "field", "senior", "company", "unpaid",
                      "package", "timing", "already applied"):
            if token in reason.lower():
                themes[token] += 1
    repeated = [t for t, n in themes.most_common(4) if n >= 2]
    if repeated:
        parts.append("Recurring objections: " + ", ".join(repeated) + ".")

    parts.append(
        "Weigh this as evidence of taste. Do NOT treat any single comment as a "
        "blanket ban — a role that is otherwise excellent can still deserve a high "
        "score. Say plainly in the reason when a posting looks like something they "
        "have rejected before.")
    return "\n".join(parts)


def digest_for(settings) -> str:
    """Digest from whatever the pipeline attached to settings, else ""."""
    rows = getattr(settings, "feedback_rows", None) or []
    return build_digest(rows)
