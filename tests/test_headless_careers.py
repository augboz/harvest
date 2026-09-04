"""Parsing rules for the JS-rendered careers fetcher.

The browser itself is not exercised here — these cover the logic that decides
what counts as a role, which is where the bugs actually were: Dyson's job links
all read "More details about <role>", and an early version threw every one of
them away because the text began with the word "More".
"""
from jobbank.fetchers.headless_careers import (
    JOB_HREF_RE, SENIOR_TITLE_RE, _clean_title, _looks_like_role, _urls_for)


def test_accessible_link_wrapper_is_stripped_not_rejected():
    """The exact bug: Dyson renders "More details about X" as the link text."""
    assert _clean_title("More details about Graduate Design Engineer") == \
        "Graduate Design Engineer"
    assert _looks_like_role(_clean_title("More details about Graduate Design Engineer"))


def test_navigation_furniture_is_matched_whole():
    for nav in ("Search", "View all jobs", "More details", "Read more", "Browse"):
        assert not _looks_like_role(_clean_title(nav)), nav
    # a real title that merely STARTS with a nav word must survive
    assert _looks_like_role("Search Quality Graduate Engineer")
    assert _looks_like_role("Application Engineer, Motors")


def test_job_hrefs_recognised_across_url_shapes():
    for href in ("/en-gb/job-description/graduate-motor-drives-engineer/",
                 "https://x.com/careers/jobs/12345",
                 "/vacancies/engineer-2027",
                 "/en/position/design-engineer"):
        assert JOB_HREF_RE.search(href), href
    for href in ("/en-gb/who-we-are/", "/cookie-policy/", "/where-we-are/europe/"):
        assert not JOB_HREF_RE.search(href), href


def test_seniority_is_dropped_at_the_source():
    """A scraped card has no description, so filters.py cannot judge seniority —
    scraping a whole board would otherwise bury the graduate roles."""
    for title in ("Senior Design Engineer", "Lead Engineer, Mechanical Design",
                  "Head of Solution Architecture", "Principal Scientist",
                  "Senior Research Manager - Motors"):
        assert SENIOR_TITLE_RE.search(title), title
    for title in ("Graduate Design Engineer (New Product Innovation)",
                  "System Solution Graduate Engineer", "Test Engineer",
                  "Software Engineering Intern 2027"):
        assert not SENIOR_TITLE_RE.search(title), title


def test_one_url_per_query_because_the_board_ignores_paging():
    site = {"url": "https://careers.dyson.com/en-gb/search-results/",
            "queries": ["graduate", "design engineer"]}
    assert _urls_for(site) == [
        "https://careers.dyson.com/en-gb/search-results/?q=graduate",
        "https://careers.dyson.com/en-gb/search-results/?q=design+engineer"]


def test_query_param_is_configurable_and_respects_an_existing_query_string():
    site = {"url": "https://x.com/jobs?loc=uk", "queries": ["grad"],
            "query_param": "keywords"}
    assert _urls_for(site) == ["https://x.com/jobs?loc=uk&keywords=grad"]


def test_a_site_with_no_queries_is_fetched_once():
    assert _urls_for({"url": "https://x.com/jobs"}) == ["https://x.com/jobs"]


def test_missing_playwright_skips_instead_of_failing_the_run(monkeypatch):
    """An optional dependency must never take the weekly run down with it."""
    import builtins

    from jobbank.fetchers import headless_careers as hc

    real_import = builtins.__import__

    def no_playwright(name, *a, **kw):
        if name.startswith("playwright"):
            raise ImportError("no playwright here")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_playwright)

    class S:
        cfg = {"headless_careers": {"sites": [{"url": "https://x.com/jobs"}]}}

    assert hc.fetch(S()) == []
