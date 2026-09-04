"""Pinpoint fetcher — Phase 2, part two.

Aston Martin F1 is the case that motivated it: a Formula 1 team on the watchlist
from the start, typed `ats: careers`, and therefore fetched by nothing at all.
"""
from __future__ import annotations

from datetime import date

import httpx
import pytest

import jobbank.fetchers as fetchers
from jobbank.fetchers import pinpoint


@pytest.fixture(autouse=True)
def no_backoff_sleep(monkeypatch):
    monkeypatch.setattr(fetchers.time, "sleep", lambda _s: None)


def _settings(sites):
    class S:
        cfg = {"pinpoint": {"sites": sites}}
    return S()


SITE = [{"name": "Aston Martin F1", "slug": "astonmartinf1"}]


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), timeout=5)


def _payload(**over):
    a = {"id": "1", "title": "Electronics Systems Engineer",
         "url": "https://astonmartinf1.pinpointhq.com/en/postings/abc",
         "location": {"city": "Silverstone", "province": "England",
                      "postal_code": "NN12 8TJ"},
         "description": "<p>Are you passionate about <b>motorsport</b> electronics?</p>",
         "key_responsibilities": "<ul><li>Design embedded systems</li></ul>",
         "skills_knowledge_expertise": "C++, real-time systems",
         "employment_type_text": "Permanent", "workplace_type_text": "On-site",
         "deadline_at": "2026-10-01T00:00:00Z"}
    a.update(over)
    return {"data": [{"attributes": a}]}


def test_it_reads_a_board(monkeypatch):
    monkeypatch.setattr(pinpoint, "client",
                        lambda browser=False: _client(
                            lambda r: httpx.Response(200, json=_payload())))
    jobs = pinpoint.fetch(_settings(SITE))
    assert len(jobs) == 1
    j = jobs[0]
    assert j.company == "Aston Martin F1"
    assert j.title == "Electronics Systems Engineer"
    assert j.url.endswith("/postings/abc")
    # "City, Region" is what enrich.normalize_location can resolve to a country
    assert j.location == "Silverstone, England"
    assert j.deadline == date(2026, 10, 1)


def test_the_whole_body_is_kept(monkeypatch):
    """filters.py needs a tech signal somewhere in title + description, and the
    interesting words are as likely to sit under "skills" as under "description"."""
    monkeypatch.setattr(pinpoint, "client",
                        lambda browser=False: _client(
                            lambda r: httpx.Response(200, json=_payload())))
    d = pinpoint.fetch(_settings(SITE))[0].description
    assert "motorsport" in d and "embedded systems" in d and "C++" in d
    assert "<p>" not in d and "<li>" not in d


def test_a_posting_missing_its_url_is_skipped(monkeypatch):
    monkeypatch.setattr(pinpoint, "client",
                        lambda browser=False: _client(
                            lambda r: httpx.Response(200, json=_payload(url=""))))
    assert pinpoint.fetch(_settings(SITE)) == []


def test_one_dead_board_does_not_lose_the_others(monkeypatch):
    def handler(request):
        if "dead" in str(request.url):
            return httpx.Response(503)
        return httpx.Response(200, json=_payload())

    monkeypatch.setattr(pinpoint, "client", lambda browser=False: _client(handler))
    jobs = pinpoint.fetch(_settings(
        [{"name": "Dead Co", "slug": "dead"}] + SITE))
    assert [j.company for j in jobs] == ["Aston Martin F1"]


def test_every_board_failing_is_reported(monkeypatch):
    monkeypatch.setattr(pinpoint, "client",
                        lambda browser=False: _client(lambda r: httpx.Response(503)))
    with pytest.raises(RuntimeError, match="every Pinpoint board failed"):
        pinpoint.fetch(_settings(SITE))


def test_no_sites_configured_is_a_skip_not_a_crash():
    assert pinpoint.fetch(_settings([])) == []
