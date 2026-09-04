"""Profile -> (filters, scorer) module selection.

Each candidate has their own hard filters and scoring rubric, and they are NOT
interchangeable: one profile's `check_type` may require an internship signal while
another's rejects one, so running a job through the wrong pair silently inverts the result.
Every entry point that filters or scores must resolve the pair through here
rather than importing `.filters`/`.scorer` directly — `pipeline.run` and
`pastein.process_paste_in` are both such entry points.

Imports are deferred into the functions so that importing this module never drags
in anthropic/rapidfuzz, and so `settings` can be imported without a cycle.
"""
from __future__ import annotations

from .settings import Settings


def filters_for(settings: Settings):
    """The hard-filter module for this profile."""
    from . import filters
    return filters


def scorer_for(settings: Settings):
    """The scoring module for this profile."""
    from . import scorer
    return scorer
