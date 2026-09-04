"""A transient upstream failure must cost one query, not a whole source.

Both cases here are real failures from a live run:

  adzuna:         HTTPStatusError: Server error '503 Service Temporarily
                  Unavailable' — raise_for_status() aborted every remaining
                  term and country, none of which had been tried yet.
  formulacareers: JSONDecodeError: Expecting value: line 1 column 1 — a 200
                  whose body was not JSON, which the bare .json() turned into
                  a dead source.
"""
from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

import jobbank.fetchers as fetchers
from jobbank.fetchers import fetch_with_retry, json_or_none
from jobbank.fetchers import adzuna, motorsport


@pytest.fixture(autouse=True)
def no_backoff_sleep(monkeypatch):
    """The backoff is real seconds; these tests only care that it retries."""
    monkeypatch.setattr(fetchers.time, "sleep", lambda _s: None)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), timeout=5)


# --------------------------------------------------------------- helpers ---
def test_retry_gives_up_and_returns_the_last_response():
    seen = []

    def handler(request):
        seen.append(request.url)
        return httpx.Response(503, text="try later")

    with _client(handler) as http:
        resp = fetch_with_retry(http, "GET", "https://x.test/", attempts=3,
                                base_delay=0)
    assert len(seen) == 3, "a transient status is retried, not accepted first time"
    assert resp is not None and resp.status_code == 503


def test_retry_stops_as_soon_as_it_works():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    with _client(handler) as http:
        resp = fetch_with_retry(http, "GET", "https://x.test/", base_delay=0)
    assert calls["n"] == 2
    assert json_or_none(resp) == {"ok": True}


def test_retry_survives_a_transport_error():
    def handler(request):
        raise httpx.ConnectTimeout("no route")

    with _client(handler) as http:
        assert fetch_with_retry(http, "GET", "https://x.test/", attempts=2,
                                base_delay=0) is None


def test_json_or_none_swallows_a_200_that_is_not_json():
    resp = httpx.Response(200, text="<html>edge error</html>")
    assert json_or_none(resp) is None
    assert json_or_none(None) is None
    assert json_or_none(httpx.Response(404, json={"a": 1})) is None


# ---------------------------------------------------------------- adzuna ---
class _Cfg(dict):
    """settings.cfg["adzuna"] plus the two attributes the fetcher reads."""


def _adzuna_settings(monkeypatch, terms, countries):
    class S:
        freshness_days = 30
        cfg = {"adzuna": {"terms": terms, "countries": countries,
                          "max_pages": 1, "results_per_page": 50}}

        def env(self, key):
            return {"ADZUNA_APP_ID": "id", "ADZUNA_APP_KEY": "key"}.get(key, "")
    return S()


def _one_result(title, key="x"):
    # the URL has to differ per country: the fetcher dedupes on redirect_url, so
    # a fixture that reuses one URL would look like a lost country
    return {"results": [{"redirect_url": f"https://jobs.test/{key}/{title}",
                         "title": title, "company": {"display_name": "Co"},
                         "location": {"display_name": "London"}, "id": "1"}]}


def test_adzuna_one_country_503_does_not_lose_the_others(monkeypatch):
    """The reported failure: gb 503s, and fr/de used to be lost with it."""
    def handler(request):
        path = str(request.url)
        if "/gb/" in path:
            return httpx.Response(503, text="Service Temporarily Unavailable")
        country = "fr" if "/fr/" in path else "de"
        return httpx.Response(200, json=_one_result("intern", country))

    monkeypatch.setattr(adzuna, "client", lambda browser=False: _client(handler))
    jobs = adzuna.fetch(_adzuna_settings(monkeypatch, ["intern"], ["gb", "fr", "de"]))
    assert [j.extra["country"] for j in jobs] == ["fr", "de"], \
        "a 503 on one country must not abort the source"


def test_adzuna_total_outage_is_still_reported(monkeypatch):
    def handler(request):
        return httpx.Response(503)

    monkeypatch.setattr(adzuna, "client", lambda browser=False: _client(handler))
    with pytest.raises(RuntimeError, match="every Adzuna query failed"):
        adzuna.fetch(_adzuna_settings(monkeypatch, ["intern"], ["gb"]))


def test_adzuna_rate_limit_still_stops_early_with_what_it_has(monkeypatch):
    seen = {"n": 0}

    def handler(request):
        seen["n"] += 1
        if seen["n"] == 1:
            return httpx.Response(200, json=_one_result("first"))
        return httpx.Response(429)

    monkeypatch.setattr(adzuna, "client", lambda browser=False: _client(handler))
    jobs = adzuna.fetch(_adzuna_settings(monkeypatch, ["intern"], ["gb", "fr", "de"]))
    assert len(jobs) == 1, "429 keeps the earlier pages and stops asking"


# -------------------------------------------------------- formulacareers ---
_FC_HTML = ('<li class="job_listing"><a href="https://formulacareers.com/j/1">'
            '<div class="position"><h3>Aerodynamics Placement</h3></div>'
            '<div class="company"><strong>Some F1 Team</strong></div>'
            '<div class="location">Brackley</div></a></li>')


@pytest.fixture
def no_curl_cffi(monkeypatch):
    """Force the plain-httpx fallback, so the MockTransport is what answers.

    _fc_page prefers curl_cffi (formulacareers edge-blocks anything that does
    not look like a browser). Without this the tests below would reach the real
    site instead of their fixtures.
    """
    import builtins
    real = builtins.__import__

    def fake(name, *a, **kw):
        if name.startswith("curl_cffi"):
            raise ImportError("not installed")
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake)


def test_formulacareers_prefers_browser_impersonation(monkeypatch):
    """curl_cffi is tried FIRST: plain httpx is what the site blocks."""
    import sys
    import types

    calls = []

    class _Resp:
        status_code = 200

        def json(self):
            return {"max_num_pages": 1, "html": _FC_HTML}

    fake_creq = types.SimpleNamespace(
        post=lambda url, **kw: (calls.append(kw.get("impersonate")), _Resp())[1])
    fake_mod = types.ModuleType("curl_cffi")
    fake_mod.requests = fake_creq
    monkeypatch.setitem(sys.modules, "curl_cffi", fake_mod)

    def handler(request):        # httpx must not be reached at all
        raise AssertionError("fell back to httpx despite curl_cffi being present")

    monkeypatch.setattr(motorsport, "client", lambda browser=False: _client(handler))
    jobs = motorsport.fetch_formulacareers(object())
    assert calls == ["chrome", "chrome"], "both categories go through curl_cffi"
    # one job, not two: the same posting listed under f1 AND motorsport is
    # deduped on URL, which is the fetcher behaving correctly
    assert [j.title for j in jobs] == ["Aerodynamics Placement"]


def test_formulacareers_non_json_200_does_not_kill_the_source(no_curl_cffi, monkeypatch):
    """f1 answers HTML (the reported JSONDecodeError); motorsport still lands."""
    def handler(request):
        body = request.content.decode()
        if "f1" in body:
            return httpx.Response(200, text="<html>blocked</html>")
        return httpx.Response(200, json={"max_num_pages": 1, "html": _FC_HTML})

    monkeypatch.setattr(motorsport, "client", lambda browser=False: _client(handler))
    jobs = motorsport.fetch_formulacareers(object())
    assert [j.title for j in jobs] == ["Aerodynamics Placement"]


def test_formulacareers_endpoint_gone_is_reported(no_curl_cffi, monkeypatch):
    def handler(request):
        return httpx.Response(200, text="not json at all")

    monkeypatch.setattr(motorsport, "client", lambda browser=False: _client(handler))
    with pytest.raises(RuntimeError, match="no usable JSON for either category"):
        motorsport.fetch_formulacareers(object())


def test_formulacareers_unreachable_is_not_a_source_failure(no_curl_cffi,
                                                            monkeypatch):
    """A connect timeout is the runner's problem, not the board's.

    2026-08-31: the nightly source check went red because formulacareers timed
    out twice from a GitHub runner while answering a laptop in two seconds. An
    endpoint we never reached tells us nothing about whether it moved.
    """
    def handler(request):
        raise httpx.ConnectTimeout("timed out", request=request)

    monkeypatch.setattr(motorsport, "client", lambda browser=False: _client(handler))
    assert motorsport.fetch_formulacareers(object()) == []


def test_formulacareers_reads_a_healthy_response(no_curl_cffi, monkeypatch):
    """Guard the happy path too, so the retry wrapper can't quietly break it."""
    def handler(request):
        return httpx.Response(200, json={"max_num_pages": 1, "html": _FC_HTML})

    monkeypatch.setattr(motorsport, "client", lambda browser=False: _client(handler))
    jobs = motorsport.fetch_formulacareers(object())
    assert len(jobs) == 1
    assert jobs[0].company == "Some F1 Team"
    assert jobs[0].url == "https://formulacareers.com/j/1"
    assert json.dumps(jobs[0].extra)   # extras stay serialisable


# ---------------------------------------------------------------- health ---
# `jobbank health` is what the scheduled check runs; its EXIT CODE is the whole
# alert. If that code is wrong the check is decorative.
class _NoKeySettings:
    """health() also pings the scoring model; with no key that is a skip, which
    keeps these tests about SOURCE failures and off the network."""

    def env(self, _key):
        return ""

def test_health_fails_when_a_source_raised(monkeypatch, capsys):
    from jobbank.__main__ import health
    from jobbank.fetchers import FetchResult

    monkeypatch.setattr(
        fetchers, "run_all",
        lambda settings, only=None: FetchResult(
            per_source={"adzuna": 12, "formulacareers": 0},
            failures={"formulacareers": "JSONDecodeError: Expecting value"}))
    assert health(_NoKeySettings()) == 1
    assert "formulacareers" in capsys.readouterr().out


def test_health_passes_when_a_source_is_merely_empty(monkeypatch, capsys):
    """A thin day is not a broken source. Failing on it teaches you to ignore
    the alert, which is worse than having no alert."""
    from jobbank.__main__ import health
    from jobbank.fetchers import FetchResult

    monkeypatch.setattr(
        fetchers, "run_all",
        lambda settings, only=None: FetchResult(per_source={"adzuna": 12, "reed": 0},
                                                failures={}))
    assert health(_NoKeySettings()) == 0
    assert "no source failures" in capsys.readouterr().out


# ----------------------------------------------------------- ats_watchlist ---
# The 2026-08-24 evening check: "[ats_watchlist] FAILED: [Errno 104] Connection
# reset by peer". One dropped connection cost all 63 watchlist boards — 4768
# jobs, the biggest source in the run.
def _watchlist_settings(entries):
    class S:
        watchlist = entries
    return S()


def test_ats_one_dead_board_does_not_lose_the_others(monkeypatch):
    from jobbank.fetchers import ats

    def handler(request):
        if "reset-co" in str(request.url):
            raise httpx.ReadError("[Errno 104] Connection reset by peer")
        return httpx.Response(200, json={"jobs": [
            {"id": 1, "title": "SWE Intern", "absolute_url": "https://x.test/1",
             "location": {"name": "London"}, "content": "<p>work</p>"}]})

    monkeypatch.setattr(ats, "client", lambda browser=False: _client(handler))
    jobs = ats.fetch(_watchlist_settings([
        {"name": "Reset Co", "ats": "greenhouse", "slug": "reset-co"},
        {"name": "Fine Co", "ats": "greenhouse", "slug": "fine-co"}]))
    assert [j.company for j in jobs] == ["Fine Co"], \
        "a reset connection on one board must not abort the sweep"


def test_ats_every_board_failing_is_reported(monkeypatch):
    from jobbank.fetchers import ats

    monkeypatch.setattr(ats, "client",
                        lambda browser=False: _client(lambda r: httpx.Response(503)))
    with pytest.raises(RuntimeError, match="every watchlist board failed"):
        ats.fetch(_watchlist_settings(
            [{"name": "A", "ats": "lever", "slug": "a"}]))


def test_ats_404_is_a_bad_slug_not_a_failure(monkeypatch):
    """A board that has moved is a config problem, logged, never fatal."""
    from jobbank.fetchers import ats

    def handler(request):
        if "gone" in str(request.url):
            return httpx.Response(404)
        return httpx.Response(200, json=[{"id": "z", "text": "Intern",
                                          "hostedUrl": "https://x.test/z",
                                          "categories": {"location": "Paris"}}])

    monkeypatch.setattr(ats, "client", lambda browser=False: _client(handler))
    jobs = ats.fetch(_watchlist_settings([
        {"name": "Gone Co", "ats": "lever", "slug": "gone"},
        {"name": "Live Co", "ats": "lever", "slug": "live"}]))
    assert [j.company for j in jobs] == ["Live Co"]


# --------------------------------------------------------- motorsportjobs ---
_MSJ_CARD = (
    '<article id="node-99">'
    '<h2><a class="recruiter-job-link" href="https://www.motorsportjobs.com/en/job/x">'
    'Junior Mechanical Design Engineer</a></h2>'
    '<div class="organization-name"><a>Red Bull Racing</a></div>'
    '<div class="location">'
    '  <span class="recruiter-epiq-icon svg-icon"><svg><title>Location</title></svg></span>'
    '  <span>Milton Keynes, UK</span></div>'
    '<span class="date"><span class="recruiter-epiq-icon svg-icon"><svg>'
    '<title>Published date</title></svg></span>01.09.2026</span>'
    '</article>')


class _Cfg:
    """Stand-in for Settings: the fetcher only reads settings.cfg."""

    def __init__(self, cfg):
        self.cfg = cfg


def test_motorsportjobs_reads_the_current_facet_paths(monkeypatch):
    """Location must not come out as the word "Location".

    div.location leads with a span wrapping an SVG whose <title> is "Location",
    so `div.location span` matched the icon and every job was located there.
    """
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, text=f"<html><body>{_MSJ_CARD}</body></html>")

    monkeypatch.setattr(motorsport, "client", lambda browser=False: _client(handler))
    jobs = motorsport.fetch_motorsportjobs(
        _Cfg({"motorsportjobs": {"paths": ["industry/formula-1-10611"],
                                 "max_pages": 1}}))
    assert len(jobs) == 1
    j = jobs[0]
    assert j.company == "Red Bull Racing"
    assert j.location == "Milton Keynes, UK"
    assert j.posted == date(2026, 9, 1)
    assert j.ats_id == "msj-99"
    assert seen == ["https://www.motorsportjobs.com/en/jobs/industry/formula-1-10611"]


def test_motorsportjobs_dead_taxonomy_is_reported(monkeypatch):
    """The failure this whole change exists to catch.

    2026-09-01: the board dropped its contract-type facets. The old paths kept
    answering 200 with the page shell and no listings, so the source read as
    healthy-but-quiet for weeks. A clean sweep of empty paths must be loud.
    """
    def handler(request):
        return httpx.Response(200, text="<html><body>no listings here</body></html>")

    monkeypatch.setattr(motorsport, "client", lambda browser=False: _client(handler))
    with pytest.raises(RuntimeError, match="facet paths have probably moved"):
        motorsport.fetch_motorsportjobs(
            _Cfg({"motorsportjobs": {"paths": ["a", "b"], "max_pages": 1}}))


def test_motorsportjobs_one_empty_facet_is_not_a_failure(monkeypatch):
    """A single facet running dry is normal and must not fail the source."""
    def handler(request):
        if "empty" in str(request.url):
            return httpx.Response(200, text="<html><body></body></html>")
        return httpx.Response(200, text=f"<html><body>{_MSJ_CARD}</body></html>")

    monkeypatch.setattr(motorsport, "client", lambda browser=False: _client(handler))
    jobs = motorsport.fetch_motorsportjobs(
        _Cfg({"motorsportjobs": {"paths": ["empty", "live"], "max_pages": 1}}))
    assert [j.title for j in jobs] == ["Junior Mechanical Design Engineer"]
