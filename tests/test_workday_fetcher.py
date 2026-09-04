"""Workday fetcher — the first half of the Phase 2 `careers` gap.

40 of the 84 watchlist companies were typed `ats: careers` and fetched by
nothing at all. Every run log said so; McLaren, Ferrari, Airbus, Thales and
every F1 team sat there untouched while the pipeline reported success.
"""
from __future__ import annotations

from datetime import date, timedelta

import httpx
import pytest

import jobbank.fetchers as fetchers
from jobbank.fetchers import workday


@pytest.fixture(autouse=True)
def no_backoff_sleep(monkeypatch):
    monkeypatch.setattr(fetchers.time, "sleep", lambda _s: None)


def _settings(sites, **over):
    class S:
        cfg = {"workday": {"sites": sites, "terms": ["intern"], "per_page": 20,
                           "max_pages": 1, "detail_cap": 10,
                           "countries": ["UK"], **over}}
    return S()


SITE = [{"name": "Airbus", "host": "ag.wd3.myworkdayjobs.com",
         "tenant": "ag", "site": "Airbus"}]


def _posting(title, loc="Bristol Area", path="/job/Bristol/Thing_JR1"):
    return {"title": title, "locationsText": loc, "externalPath": path,
            "postedOn": "Posted Today", "bulletFields": ["JR1"]}


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), timeout=5)


def test_it_finds_roles_and_builds_the_public_url(monkeypatch):
    def handler(request):
        if "/wday/cxs/ag/Airbus/jobs" in str(request.url):
            return httpx.Response(200, json={"jobPostings": [
                _posting("Software Engineering Intern")]})
        return httpx.Response(200, json={"jobPostingInfo": {
            "jobDescription": "<p>Build <b>things</b> in Python.</p>"}})

    monkeypatch.setattr(workday, "client", lambda browser=False: _client(handler))
    jobs = workday.fetch(_settings(SITE))
    assert len(jobs) == 1
    j = jobs[0]
    assert j.company == "Airbus"
    # the browsable page, not the API path — Aug has to be able to click it
    assert j.url == "https://ag.wd3.myworkdayjobs.com/Airbus/job/Bristol/Thing_JR1"
    assert j.description == "Build things in Python."
    assert j.posted == date.today()


def test_titles_that_are_not_early_careers_are_skipped(monkeypatch):
    """"industrial" alone matched "Industrial Process Engineer" and "Industrial
    Architect" — both senior staff roles that came back from a UK search."""
    def handler(request):
        if "/jobs" in str(request.url):
            return httpx.Response(200, json={"jobPostings": [
                _posting("Industrial Process Engineer", path="/job/a"),
                _posting("Industrial Architect", path="/job/b"),
                _posting("Senior Network Engineer", path="/job/c"),
                _posting("Industrial Placement - Software", path="/job/d"),
                _posting("SI&T IVVQ Intern", path="/job/e")]})
        return httpx.Response(200, json={"jobPostingInfo": {"jobDescription": "x"}})

    monkeypatch.setattr(workday, "client", lambda browser=False: _client(handler))
    titles = [j.title for j in workday.fetch(_settings(SITE))]
    assert titles == ["Industrial Placement - Software", "SI&T IVVQ Intern"]


def test_a_tenant_that_rejects_the_country_filter_still_works(monkeypatch):
    """NVIDIA's wd5 tenant 400s on the facet. Losing the company over that would
    be worse than searching it unfiltered and letting the later filters work."""
    seen = {"faceted": 0, "plain": 0}

    def handler(request):
        if "/jobs" in str(request.url):
            body = request.content.decode()
            if "locationCountry" in body and '"locationCountry": []' not in body:
                seen["faceted"] += 1
                return httpx.Response(400, text="bad facet")
            seen["plain"] += 1
            return httpx.Response(200, json={"jobPostings": [_posting("Data Intern")]})
        return httpx.Response(200, json={"jobPostingInfo": {"jobDescription": "x"}})

    monkeypatch.setattr(workday, "client", lambda browser=False: _client(handler))
    jobs = workday.fetch(_settings(SITE))
    assert seen["faceted"] >= 1 and seen["plain"] >= 1
    assert [j.title for j in jobs] == ["Data Intern"]


def test_descriptions_are_capped(monkeypatch):
    """A 2000-job tenant must not mean 2000 extra requests for a handful of roles."""
    details = {"n": 0}

    def handler(request):
        if "/jobs" in str(request.url):
            return httpx.Response(200, json={"jobPostings": [
                _posting(f"Intern {i}", path=f"/job/{i}") for i in range(20)]})
        details["n"] += 1
        return httpx.Response(200, json={"jobPostingInfo": {"jobDescription": "x"}})

    monkeypatch.setattr(workday, "client", lambda browser=False: _client(handler))
    jobs = workday.fetch(_settings(SITE, detail_cap=3))
    assert len(jobs) == 20, "every matching role is kept"
    assert details["n"] == 3, "but only the cap is opened for a description"


def test_a_dead_tenant_is_reported_not_silently_empty(monkeypatch):
    monkeypatch.setattr(workday, "client",
                        lambda browser=False: _client(lambda r: httpx.Response(503)))
    with pytest.raises(RuntimeError, match="every Workday query failed"):
        workday.fetch(_settings(SITE))


def test_posted_dates():
    assert workday._posted_date("Posted Today") == date.today()
    assert workday._posted_date("Posted Yesterday") == date.today() - timedelta(days=1)
    assert workday._posted_date("Posted 30+ Days Ago") == date.today() - timedelta(days=30)
    assert workday._posted_date("") is None


def test_missing_tenant_details_are_skipped_not_crashed(monkeypatch):
    monkeypatch.setattr(workday, "client",
                        lambda browser=False: _client(lambda r: httpx.Response(200, json={})))
    assert workday.fetch(_settings([{"name": "Half configured", "host": "x.test"}])) == []
