"""admit_all: a good role gets in however full the list is, and NOTHING is evicted.

The caps were archiving roles that scored 78-82 for want of space while the bank
held roles the LLM later scored in the forties — first-come-first-served, with
score used only to rank the newcomers against each other. Turning the size caps
off fixes that without touching a single row already in the bank, which is the
explicit constraint: nothing leaves.
"""
from __future__ import annotations

from jobbank.models import Job
from jobbank.volume import volume_gate


def _job(title, score, company="Co", **extra):
    j = Job(source="t", company=company, title=title,
            url=f"https://jobs.test/{title.replace(' ', '-')}-{company}",
            location="London, UK")
    j.score = score
    j.extra.update(extra)
    return j


def test_a_full_bank_no_longer_turns_a_good_role_away():
    """No slots, no headroom — the exact state that archived Samsara at 83."""
    jobs = [_job("SWE Intern", 83, "Samsara"), _job("ML Intern", 78, "Perplexity")]
    blocked = volume_gate(jobs, slots=0, min_score=75, priority_score=85, headroom=0)
    assert blocked.bank == [], "precondition: the caps used to archive both"

    opened = volume_gate(jobs, slots=0, min_score=75, priority_score=85,
                         headroom=0, admit_all=True)
    assert [j.company for j in opened.bank] == ["Samsara", "Perplexity"]
    assert opened.archive == []


def test_admit_all_never_evicts_anything():
    """The gate only ever decides about the jobs handed to it. Rows already in
    the bank are not passed in and cannot be touched — that is the guarantee
    behind "don't swap anything from my job bank"."""
    jobs = [_job("SWE Intern", 95, "Haas F1")]
    res = volume_gate(jobs, slots=0, min_score=75, priority_score=85,
                      headroom=0, admit_all=True)
    # bank + archive together account for every job in, and nothing else exists
    assert len(res.bank) + len(res.archive) == len(jobs)
    assert res.bank[0].company == "Haas F1"


def test_the_score_bar_still_holds():
    """admit_all removes the SIZE caps, not the quality gate. The Auto Trader HR
    role scored 15; it must not walk in through the open door."""
    jobs = [_job("Early Careers Advisor", 15, "Autotrader"),
            _job("SWE Intern", 80, "Good Co")]
    res = volume_gate(jobs, slots=0, min_score=75, priority_score=85,
                      headroom=0, admit_all=True)
    assert [j.company for j in res.bank] == ["Good Co"]
    assert [j.company for j in res.archive] == ["Autotrader"]


def test_one_employer_still_cannot_take_over():
    """max_per_company survives: nine of the 31 rows were TikTok once."""
    jobs = [_job(f"SWE Intern {i}", 90, "TikTok") for i in range(6)]
    res = volume_gate(jobs, slots=0, min_score=75, priority_score=85,
                      headroom=0, admit_all=True, max_per_company=3)
    assert len(res.bank) == 3
    assert len(res.archive) == 3


def test_best_first_when_a_company_cap_bites():
    """Ranking still matters: if only three TikTok roles fit, they are the three
    best ones, not the first three off the wire."""
    jobs = [_job("Weak", 76, "TikTok"), _job("Best", 94, "TikTok"),
            _job("Good", 88, "TikTok"), _job("Fine", 80, "TikTok")]
    res = volume_gate(jobs, slots=0, min_score=75, priority_score=85,
                      headroom=0, admit_all=True, max_per_company=3)
    assert [j.title for j in res.bank] == ["Best", "Good", "Fine"]


def test_the_old_behaviour_is_untouched_by_default():
    """A profile that does not set admit_all_qualifying keeps its caps
    working exactly as before — even a bank already well over its ceiling."""
    jobs = [_job("A", 90), _job("B", 88), _job("C", 86)]
    res = volume_gate(jobs, slots=1, min_score=60, priority_score=95, headroom=1)
    assert len(res.bank) == 1
    assert len(res.archive) == 2


# ---------------------------------------------------------- watchlist relief ---
def _wjob(company, score):
    from jobbank.models import Job
    j = Job(source="t", company=company, title="SWE Intern", url=f"https://x/{company}/{score}")
    j.score = score
    return j


def test_a_watched_company_banks_below_the_general_bar():
    """Sony's intern roles scored 56-72 against a bar of 75 and were archived
    for weeks. Aug: "these are the sort of jobs i want to hear about"."""
    from jobbank.volume import volume_gate
    sony, rando = _wjob("Sony Electronics", 62), _wjob("Randomly Fine Corp", 62)
    gate = volume_gate([sony, rando], 5, 75, 85, admit_all=True,
                       watchlist={"sony electronics"}, watchlist_relief=15)
    assert sony in gate.bank
    assert rando not in gate.bank        # same score, no relief — bar stays 75


def test_relief_does_not_lower_the_floor_to_nothing():
    from jobbank.volume import volume_gate
    sony = _wjob("Sony Electronics", 59)
    gate = volume_gate([sony], 5, 75, 85, admit_all=True,
                       watchlist={"sony electronics"}, watchlist_relief=15)
    assert sony not in gate.bank          # 59 < 75-15


def test_relief_zero_changes_nothing():
    from jobbank.volume import volume_gate
    sony = _wjob("Sony Electronics", 74)
    gate = volume_gate([sony], 5, 75, 85, admit_all=True,
                       watchlist={"sony electronics"}, watchlist_relief=0)
    assert sony not in gate.bank


def test_max_per_company_still_binds_relieved_roles():
    """Relief lowers the bar, never the flood defences: PlayStation's January
    intern drop must not take the whole bank at 60+."""
    from jobbank.volume import volume_gate
    jobs = [_wjob("Sony Electronics", 60 + i) for i in range(6)]
    gate = volume_gate(jobs, 99, 75, 85, admit_all=True, max_per_company=3,
                       watchlist={"sony electronics"}, watchlist_relief=15)
    assert len(gate.bank) == 3
