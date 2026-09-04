from datetime import date, timedelta

from jobbank.filters import FilterStats, apply_hard_filters
from jobbank.models import Job, OpeningSoonEntry
from jobbank.enrich import extract_duration, extract_start
from jobbank.opening_soon import DUE_STATUS, sync_opening_soon
from jobbank.fetchers.motorsport import _rel_date

TODAY = date(2026, 7, 15)


# --- quant firm blocklist (D.E. Shaw etc. under a Tech tag) -----------------
def test_quant_firm_blocked():
    j = Job(source="trackr", company="D.E. Shaw", title="Systems Engineering Intern 2027",
            url="https://x.com/1", description="Software systems internship.")
    _, rejected = apply_hard_filters([j], 45, FilterStats(), today=TODAY)
    assert rejected and "quant" in rejected[0][1]

def test_jane_street_blocked():
    j = Job(source="t", company="Jane Street Capital", title="Software Engineer Intern",
            url="https://x.com/2")
    _, rejected = apply_hard_filters([j], 45, FilterStats(), today=TODAY)
    assert rejected and "quant" in rejected[0][1]

def test_consulting_excluded():
    for title in ["Intern AI & Management Consulting (Value Engineering)",
                  "Technology Consultant Placement", "Business Analyst Intern"]:
        j = Job(source="t", company="Celonis", title=title, url="https://x.com/1",
                description="AI and software.")
        _, rejected = apply_hard_filters([j], 45, FilterStats(), today=TODAY)
        assert rejected and "consulting" in rejected[0][1], title


def test_normal_firm_not_blocked():
    j = Job(source="t", company="McLaren Racing", title="Software Engineer Placement 2027",
            url="https://x.com/3")
    passed, _ = apply_hard_filters([j], 45, FilterStats(), today=TODAY)
    assert len(passed) == 1


# --- timing enrichment ------------------------------------------------------
def test_extract_duration():
    assert extract_duration("12 month placement") == "12 months"
    assert extract_duration("Year in industry software") == "12 months"
    assert extract_duration("6-month internship") == "6 months"
    assert extract_duration("Summer software intern") == "~10-12 weeks (summer)"
    # never blank when we at least know it's an internship/placement
    assert extract_duration("Software Engineer Intern") == "Internship (TBC)"
    assert extract_duration("Software Engineer") == ""

def test_extract_start():
    assert extract_start("Summer 2027 internship") == "Summer 2027"
    assert extract_start("Placement starting September 2027") == "September 2027"
    assert "2027" in extract_start("Graduate placement 2027")


def test_rel_date():
    assert _rel_date("11h ago", TODAY) == TODAY
    assert _rel_date("2 days ago", TODAY) == TODAY - timedelta(days=2)
    assert _rel_date("3w ago", TODAY) == TODAY - timedelta(weeks=3)


# --- Opening Soon sync + promotion -----------------------------------------
class FakeBackend:
    def __init__(self, rows):
        self._rows = rows
        self.appended = []
        self.status_changes = []

    def read_opening_soon(self):
        return self._rows

    def append_opening_soon(self, entries):
        self.appended.extend(entries)

    def set_opening_soon_status(self, row_index, status, notes):
        self.status_changes.append((row_index, status))


def test_opening_soon_adds_new_and_dedupes():
    existing = [{"row": 2, "Company": "McLaren Racing", "Programme": "Industrial Placement",
                 "Expected open date": "2026-09-15", "Status": "Announced"}]
    be = FakeBackend(existing)
    seed = [OpeningSoonEntry(company="McLaren Racing", programme="Industrial Placement"),  # dup
            OpeningSoonEntry(company="Williams Racing", programme="Placement Year")]        # new
    added, now_open = sync_opening_soon(be, seed, [], today=TODAY)
    assert added == 1
    assert be.appended[0].company == "Williams Racing"
    assert now_open == []  # McLaren opens 2026-09-15, still future


def test_opening_soon_flips_when_a_posting_confirms_it():
    """The open date passing is not evidence — most of them are guesses. What
    flips a row is a matching posting turning up in the run."""
    existing = [{"row": 2, "Company": "Google", "Programme": "SWE Intern 2027",
                 "Expected open date": "2026-07-01", "Status": "Predicted from last year"}]
    be = FakeBackend(existing)
    jobs = [Job(source="t", company="Google", title="Software Engineering Intern 2027",
                url="https://careers.google/1")]
    added, now_open = sync_opening_soon(be, [], [], today=TODAY, jobs=jobs)
    assert be.status_changes == [(2, "OPEN NOW")]
    assert now_open == ["Google — SWE Intern 2027"]


def test_opening_soon_does_not_flip_on_the_date_alone():
    existing = [{"row": 2, "Company": "Google", "Programme": "SWE Intern 2027",
                 "Expected open date": "2026-07-01", "Status": "Predicted from last year"}]
    be = FakeBackend(existing)
    _, now_open = sync_opening_soon(be, [], [], today=TODAY, jobs=[])
    assert now_open == []
    assert be.status_changes == [(2, DUE_STATUS)]


def test_opening_soon_added_by_hand_still_gets_flipped():
    """A programme Aug tracked himself from the dashboard must behave exactly like a
    seeded one — that flip is what puts it at the top of the digest email. It needs
    the same evidence a seeded row does; a hand-typed date is still a prediction."""
    existing = [{"row": 4, "Company": "European Space Agency",
                 "Programme": "Graduate Trainee Programme 2027",
                 "Expected open date": "2026-07-01", "Status": "Added by hand"}]
    be = FakeBackend(existing)
    jobs = [Job(source="t", company="European Space Agency",
                title="Young Graduate Trainee Programme 2027", url="https://esa/1")]
    _, now_open = sync_opening_soon(be, [], [], today=TODAY, jobs=jobs)
    assert be.status_changes == [(4, "OPEN NOW")]
    assert now_open == ["European Space Agency — Graduate Trainee Programme 2027"]


def test_dismissed_opening_soon_is_not_resurrected():
    """Dismissing from the dashboard keeps the row as a record; flipping it back to
    OPEN NOW would undo a deliberate decision."""
    existing = [{"row": 2, "Company": "Google", "Programme": "SWE Intern 2027",
                 "Expected open date": "2026-07-01",
                 "Status": "Dismissed (was: Predicted from last year)"}]
    be = FakeBackend(existing)
    _, now_open = sync_opening_soon(be, [], [], today=TODAY)
    assert be.status_changes == []
    assert now_open == []


def test_gsa_capital_quant_blocked():
    """A quant fund with a plain SWE title and no quant keywords must still be blocked."""
    job = Job(source="adzuna", company="GSA Capital", title="Software Developer - Intern",
              url="https://x.com/gsa", description="Software development internship.")
    _, rejected = apply_hard_filters([job], 45, FilterStats(), today=TODAY)
    assert rejected and "quant" in rejected[0][1]


def test_fund_shaped_company_names_blocked():
    for co in ["Marshall Wace", "Acme Asset Management", "Foo Capital Partners"]:
        job = Job(source="t", company=co, title="Software Engineer Intern",
                  url="https://x.com/1", description="Software.")
        _, rejected = apply_hard_filters([job], 45, FilterStats(), today=TODAY)
        assert rejected and "quant" in rejected[0][1], co


def test_capital_one_blocked_as_finance_not_as_quant():
    """It is a bank, not a trading shop. It used to be let through because the
    quant rule was the only finance-shaped filter; Aug has since said no finance
    at all (2026-08-18), so it goes — but for the right reason."""
    job = Job(source="t", company="Capital One", title="Software Engineer Intern 2027",
              url="https://x.com/c1", description="Software engineering internship.")
    passed, rejected = apply_hard_filters([job], 45, FilterStats(), today=TODAY)
    assert not passed
    assert "finance employer" in rejected[0][1]


def test_short_firm_names_do_not_match_inside_words():
    """"sig" (SIG) matched De-sig-n Studio and "ing" (ING) matched McLaren
    Rac-ing, so both were silently dropped as quant firms."""
    for company in ("Design Studio", "Signal AI", "Sigma Design", "McLaren Racing"):
        job = Job(source="t", company=company, title="Software Engineer Intern 2027",
                  url=f"https://x.com/{company}", description="Software engineering internship.")
        passed, rejected = apply_hard_filters([job], 45, FilterStats(), today=TODAY)
        assert passed, f"{company} was dropped: {rejected and rejected[0][1]}"


def test_finance_and_fintech_are_both_excluded():
    """Fintech was allowed through while Stripe and Monzo were on the watchlist.
    Both came off on 2026-08-18 ("really no finance"), so the exception went too —
    but the block is on payments companies, not on any name that resembles one:
    Square Enix and Mercury Systems are not banks."""
    def blocked(company):
        job = Job(source="t", company=company, title="Software Engineer Intern 2027",
                  url=f"https://x.com/{company}", description="Software engineering internship.")
        passed, _ = apply_hard_filters([job], 45, FilterStats(), today=TODAY)
        return not passed
    for company in ("Goldman Sachs", "JPMorganChase", "Vanguard", "BNP Paribas",
                    "Bank of China USA", "Kentucky Farm Bureau Insurance",
                    "Bristol Capital Ltd."):
        assert blocked(company), company
    for company in ("Stripe", "Monzo", "Revolut", "Wise", "PayPal"):
        assert blocked(company), company
    for company in ("Figma", "Databank Inc", "Square Enix", "Mercury Systems",
                    "Blockchain Labs", "Databricks"):
        assert not blocked(company), company


def test_non_software_admin_roles_excluded():
    for title in ["Urban Design Learning Administrator",
                  "Customer Solution Architect - Graduate"]:
        job = Job(source="t", company="TfL", title=title, url="https://x.com/2",
                  description="Placement scheme.")
        _, rejected = apply_hard_filters([job], 45, FilterStats(), today=TODAY)
        assert rejected, title


def test_commodity_trading_house_blocked():
    job = Job(source="t", company="Castleton Commodities International",
              title="Data Engineering Internship (Summer 2027)", url="https://x.com/cc",
              description="Data engineering internship.")
    _, rejected = apply_hard_filters([job], 45, FilterStats(), today=TODAY)
    assert rejected and "quant" in rejected[0][1]


def test_watchlist_matching_is_token_aware():
    """Exact equality silently failed for real targets — regression guard."""
    from jobbank.models import matches_watchlist
    wl = {"mistral", "sony / playstation", "haas f1", "google", "spotify"}
    for co in ["Mistral AI", "Mistral Ai", "Sony", "Haas F1 Team", "Google Ltd",
               "Spotify AB"]:
        assert matches_watchlist(co, wl), co
    for co in ["Random Agency Ltd", "Eligo Recruitment", "Lucy Group"]:
        assert not matches_watchlist(co, wl), co


def test_location_normalisation():
    from jobbank.enrich import normalize_location as n
    assert n("Banbury, Oxfordshire, United Kingdom") == "Banbury, UK"
    assert n("London, England, GB") == "London, UK"
    assert n("Edinburgh, Scotland, GB") == "Edinburgh, UK"
    assert n("Paris") == "Paris, France"
    assert n("Mountain View, CA") == "Mountain View, USA"      # CA = California
    assert n("Waterloo, ON, Canada") == "Waterloo, Canada"
    assert n("Remote - USA") == "Remote, USA"
    assert n("London, undefined, GB") == "London, UK"
    assert n("UK") == "UK"
    assert n("LE175QS") == "LE175QS"        # unparseable -> keep, never blank
    assert n("") == ""


def test_digest_send_policy():
    """Daily cron must not mean daily email: only send when there's something to
    say, plus a guaranteed Monday summary."""
    from datetime import date as _d

    def should_send(added, now_open, failures, today):
        urgent = [j for j in added if j.deadline and 0 <= (j.deadline - today).days <= 7]
        return bool(added or now_open or urgent or failures or today.weekday() == 0)

    wed, mon = _d(2026, 7, 29), _d(2026, 7, 27)
    assert wed.weekday() == 2 and mon.weekday() == 0
    assert not should_send([], [], {}, wed)            # quiet Wednesday -> silence
    assert should_send([], [], {}, mon)                # Monday summary always
    job = Job(source="t", company="X", title="Intern", url="https://x")
    assert should_send([job], [], {}, wed)             # new role -> send
    assert should_send([], ["Google — SWE"], {}, wed)  # NOW OPEN -> send
    assert should_send([], [], {"reed": "boom"}, wed)  # source failure -> send


def test_linkcheck_only_drops_definitively_dead_links():
    from jobbank.linkcheck import canonical_url
    # apply-form URLs are normalised to the posting itself
    assert canonical_url("https://jobs.lever.co/x/abc/apply") == "https://jobs.lever.co/x/abc"
    assert canonical_url("https://jobs.lever.co/x/abc") == "https://jobs.lever.co/x/abc"
    # a 403 or timeout must NOT be read as "job is gone" (ATS hosts block bots)
    from jobbank import linkcheck
    assert linkcheck.DEAD_STATUSES == {404, 410}
