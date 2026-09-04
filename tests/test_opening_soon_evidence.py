"""A tracked programme is only "OPEN NOW" when a posting says so.

Most Opening Soon rows carry a guessed date — the seed labels them "Predicted
from last year". Flipping on the date alone announced five programmes as open on
1 September 2026 on no evidence at all, and the digest led with them.
"""
from datetime import date

from jobbank.models import Job
from jobbank.opening_soon import DUE_STATUS, sync_opening_soon
from jobbank.opening_soon_evidence import find_live_posting, same_employer

TODAY = date(2026, 9, 1)


def _row(company, programme, opens, status="Predicted from last year", row=2):
    return {"row": row, "Company": company, "Programme": programme,
            "Expected open date": opens, "Status": status}


class FakeBackend:
    def __init__(self, rows):
        self.rows = rows
        self.writes = []

    def read_opening_soon(self):
        return self.rows

    def append_opening_soon(self, entries):
        pass

    def set_opening_soon_status(self, row_index, status, notes):
        self.writes.append((row_index, status, notes))


def _job(company, title, url="https://x/1"):
    return Job(source="t", company=company, title=title, url=url)


# --- the employer test ------------------------------------------------------

def test_the_same_employer_written_two_ways_matches():
    assert same_employer("Red Bull Racing", "Red Bull Technology")
    assert same_employer("Aston Martin F1", "Aston Martin Aramco Formula 1 Team")


def test_a_different_company_sharing_one_word_does_not():
    # the whole point: McLaren Automotive is not McLaren Racing
    assert not same_employer("McLaren Racing", "McLaren Automotive")


# --- the kind test ----------------------------------------------------------

def test_any_old_vacancy_is_not_proof_the_placement_opened():
    jobs = [_job("McLaren", "Senior Aerodynamicist")]
    assert find_live_posting("McLaren Racing", "Industrial Placement", jobs) is None


def test_a_matching_placement_is_proof():
    jobs = [_job("McLaren", "Industrial Placement - Software Engineering")]
    hit = find_live_posting("McLaren Racing", "Industrial Placement (Software)", jobs)
    assert hit is not None and hit.title.startswith("Industrial Placement")


# --- the flip ---------------------------------------------------------------

def test_the_date_alone_no_longer_announces_an_opening():
    back = FakeBackend([_row("McLaren Racing", "Industrial Placement (Software)",
                             "2026-09-01")])
    _, alerts = sync_opening_soon(back, [], today=TODAY, jobs=[])
    assert alerts == []
    assert back.writes == [(2, DUE_STATUS, back.writes[0][2])]
    assert "no matching posting" in back.writes[0][2]


def test_a_real_posting_does_announce_it_and_says_which():
    jobs = [_job("McLaren", "Industrial Placement (Software Engineering)",
                 "https://mclaren/jobs/123")]
    back = FakeBackend([_row("McLaren Racing", "Industrial Placement (Software)",
                             "2026-09-01")])
    _, alerts = sync_opening_soon(back, [], today=TODAY, jobs=jobs)
    assert alerts == ["McLaren Racing — Industrial Placement (Software)"]
    row_index, status, note = back.writes[0]
    assert status == "OPEN NOW"
    assert "https://mclaren/jobs/123" in note and "Confirmed open" in note


def test_a_row_already_deferred_is_not_rewritten_every_run():
    back = FakeBackend([_row("McLaren Racing", "Industrial Placement",
                             "2026-09-01", status=DUE_STATUS)])
    sync_opening_soon(back, [], today=TODAY, jobs=[])
    assert back.writes == []


def test_a_deferred_row_still_flips_once_the_posting_shows_up():
    jobs = [_job("McLaren", "Industrial Placement Software")]
    back = FakeBackend([_row("McLaren Racing", "Industrial Placement",
                             "2026-09-01", status=DUE_STATUS)])
    _, alerts = sync_opening_soon(back, [], today=TODAY, jobs=jobs)
    assert len(alerts) == 1 and back.writes[0][1] == "OPEN NOW"


def test_a_future_date_is_left_alone():
    back = FakeBackend([_row("Google", "SWE Intern 2027", "2026-12-01")])
    _, alerts = sync_opening_soon(back, [], today=TODAY, jobs=[])
    assert alerts == [] and back.writes == []


def test_dismissed_rows_are_never_resurrected():
    jobs = [_job("Google", "Software Engineering Internship 2027")]
    back = FakeBackend([_row("Google", "SWE Intern 2027", "2026-09-01",
                             status="Dismissed by Aug")])
    _, alerts = sync_opening_soon(back, [], today=TODAY, jobs=jobs)
    assert alerts == [] and back.writes == []


# --- the confirmation carries score + location; OPEN NOW is re-verified -----

def _jobloc(company, title, url="https://x/1", location="", score=None):
    j = Job(source="t", company=company, title=title, url=url, location=location)
    j.score = score
    return j


def test_a_confirmation_carries_location_and_score():
    jobs = [_jobloc("Google", "Software Engineering Internship 2027",
                    "https://g/1", location="London, UK", score=81)]
    back = FakeBackend([_row("Google", "SWE Intern 2027", "2026-09-01")])
    sync_opening_soon(back, [], today=TODAY, jobs=jobs)
    note = back.writes[0][2]
    assert "| loc London, UK |" in note and "| score 81 |" in note
    assert "| seen 2026-09-01" in note


def test_an_unscored_hit_is_scored_on_the_spot():
    """Aug: "make sure they are scored". The evidence pass used to run before
    scoring, so every confirmation was unscored by construction."""
    jobs = [_jobloc("Google", "Software Engineering Internship 2027")]
    scored = []

    def score_one(j):
        scored.append(j.title)
        return 77

    back = FakeBackend([_row("Google", "SWE Intern 2027", "2026-09-01")])
    sync_opening_soon(back, [], today=TODAY, jobs=jobs, score_job=score_one)
    assert scored == ["Software Engineering Internship 2027"]
    assert "| score 77 |" in back.writes[0][2]


def test_an_open_row_refreshes_its_url_when_the_posting_moves():
    old_note = ("Confirmed open on 2026-08-15: found 'SWE Intern' at "
                "https://linkedin/dead | seen 2026-08-15")
    row = _row("Google", "SWE Intern 2027", "2026-09-01", status="OPEN NOW")
    row["Notes"] = old_note
    jobs = [_jobloc("Google", "Software Engineering Internship 2027",
                    "https://g/fresh", location="London", score=80)]
    back = FakeBackend([row])
    _, alerts = sync_opening_soon(back, [], today=TODAY, jobs=jobs)
    assert alerts == []                      # a refresh is not a new opening
    _, status, note = back.writes[0]
    assert status == "OPEN NOW"
    assert "https://g/fresh" in note
    assert "Confirmed open on 2026-08-15" in note   # first-seen date survives


def test_an_unchanged_open_row_is_not_rewritten():
    jobs = [_jobloc("Google", "Software Engineering Internship 2027",
                    "https://g/1", location="London", score=80)]
    back = FakeBackend([_row("Google", "SWE Intern 2027", "2026-09-01")])
    sync_opening_soon(back, [], today=TODAY, jobs=jobs)
    row = back.rows[0]
    row["Status"], row["Notes"] = back.writes[0][1], back.writes[0][2]
    back.writes = []
    sync_opening_soon(back, [], today=TODAY, jobs=jobs)
    assert back.writes == []


def test_one_missed_run_does_not_declare_the_posting_gone():
    row = _row("Google", "SWE Intern 2027", "2026-09-01", status="OPEN NOW")
    row["Notes"] = ("Confirmed open on 2026-08-30: found 'SWE Intern' at "
                    "https://g/1 | seen 2026-08-31")
    back = FakeBackend([row])
    sync_opening_soon(back, [], today=TODAY, jobs=[])
    assert back.writes == []                 # 1 day of absence is a CI blip


def test_a_posting_gone_for_days_says_so_instead_of_linking_to_it():
    """Aug clicked "Open the posting" and got "Job not found". The row was
    confirmed once and trusted forever; now absence has a voice."""
    from jobbank.opening_soon import GONE_STATUS
    row = _row("Google", "SWE Intern 2027", "2026-09-01", status="OPEN NOW")
    row["Notes"] = ("Confirmed open on 2026-08-15: found 'SWE Intern' at "
                    "https://linkedin/dead | seen 2026-08-25")
    back = FakeBackend([row])
    sync_opening_soon(back, [], today=TODAY, jobs=[])
    _, status, note = back.writes[0]
    assert status == GONE_STATUS
    assert "OPEN NOW" not in status          # the dashboard live-matches on that
    assert "Not seen since 2026-08-25" in note
    assert "https://linkedin/dead" in note   # kept as the record, not the CTA


def test_a_gone_row_is_reconfirmed_when_the_posting_returns():
    from jobbank.opening_soon import GONE_STATUS
    row = _row("Google", "SWE Intern 2027", "2026-09-01", status=GONE_STATUS)
    row["Notes"] = "Not seen since 2026-08-25; the posting has probably closed."
    jobs = [_jobloc("Google", "Software Engineering Internship 2027",
                    "https://g/back", score=80)]
    back = FakeBackend([row])
    _, alerts = sync_opening_soon(back, [], today=TODAY, jobs=jobs)
    assert back.writes[0][1] == "OPEN NOW"
    assert "https://g/back" in back.writes[0][2]
    assert len(alerts) == 1                  # it genuinely reopened


def test_a_gone_row_does_not_churn_back_to_due():
    from jobbank.opening_soon import GONE_STATUS
    row = _row("Google", "SWE Intern 2027", "2026-09-01", status=GONE_STATUS)
    back = FakeBackend([row])
    sync_opening_soon(back, [], today=TODAY, jobs=[])
    assert back.writes == []


def test_the_best_matching_posting_wins_not_the_first():
    """Mercedes' software placement row was confirmed by an HR posting that
    merely came first in fetch order."""
    jobs = [_job("Mercedes-AMG F1", "HR Placement - Compensation and Benefits",
                 "https://m/hr"),
            _job("Mercedes-AMG F1", "Software Engineering Industrial Placement",
                 "https://m/swe")]
    hit = find_live_posting("Mercedes-AMG F1",
                            "Industrial Placement (Software/Data)", jobs)
    assert hit is not None and hit.url == "https://m/swe"


# --- "not this posting": the dashboard's skip list is honoured ---------------

def test_a_skipped_posting_is_never_reconfirmed():
    """Aug X-ed the PhD posting; tomorrow's run must not hand it back."""
    row = _row("Google", "SWE Intern 2027", "2026-09-01", status="Due to open")
    row["Notes"] = ("Not this one: skipped https://g/phd on 2026-09-01; "
                    "still watching for the next posting. | skip https://g/phd")
    jobs = [_jobloc("Google", "Software Engineering Intern, PhD, Summer 2027",
                    "https://g/phd", score=80)]
    back = FakeBackend([row])
    _, alerts = sync_opening_soon(back, [], today=TODAY, jobs=jobs)
    assert alerts == [] and back.writes == []


def test_the_next_best_posting_replaces_a_skipped_one():
    row = _row("Google", "SWE Intern 2027", "2026-09-01", status="Due to open")
    row["Notes"] = "whatever came before | skip https://g/phd"
    jobs = [_jobloc("Google", "Software Engineering Intern, PhD, Summer 2027",
                    "https://g/phd", score=80),
            _jobloc("Google", "Software Engineering Intern, BS/MS, Summer 2027",
                    "https://g/bsms", score=85)]
    back = FakeBackend([row])
    _, alerts = sync_opening_soon(back, [], today=TODAY, jobs=jobs)
    assert len(alerts) == 1
    note = back.writes[0][2]
    assert "https://g/bsms" in note
    # the rejection must survive the rewrite, or the PhD one returns in a week
    assert "| skip https://g/phd" in note


def test_the_skip_list_survives_an_open_now_refresh():
    row = _row("Google", "SWE Intern 2027", "2026-09-01", status="OPEN NOW")
    row["Notes"] = ("Confirmed open on 2026-08-21: found 'BS/MS Intern' at "
                    "https://g/old | seen 2026-08-30 | skip https://g/phd")
    jobs = [_jobloc("Google", "Software Engineering Intern, BS/MS, Summer 2027",
                    "https://g/new", score=85)]
    back = FakeBackend([row])
    sync_opening_soon(back, [], today=TODAY, jobs=jobs)
    assert "| skip https://g/phd" in back.writes[0][2]


def test_a_landing_page_cannot_confirm_an_opening():
    """A careers URL proves the company has a website, not that the programme
    opened. Only a real posting flips a row."""
    jobs = [_job("Apple", "Software Engineering Internship",
                 "https://jobs.apple.com/en-gb/search?key=internship")]
    assert find_live_posting("Apple", "SWE Intern 2027", jobs) is None
    jobs.append(_job("Apple", "Software Engineering Internship",
                     "https://jobs.apple.com/en-gb/details/200554321/swe-intern"))
    hit = find_live_posting("Apple", "SWE Intern 2027", jobs)
    assert hit is not None and "/details/" in hit.url
