from jobbank.models import Job
from jobbank.state import State
from jobbank.volume import company_key, open_slots, volume_gate


def j(company="Acme", title="SWE Intern", url="https://x.com/a", score=None, **kw):
    job = Job(source="t", company=company, title=title, url=url, **kw)
    job.score = score
    return job


def test_exact_dedupe_ignores_tracking_params(tmp_path):
    s = State(tmp_path / "s.db")
    s.record(j(url="https://boards.greenhouse.io/acme/jobs/123?utm_source=github"), "bank")
    assert s.is_seen_exact(j(url="https://boards.greenhouse.io/acme/jobs/123?utm_source=linkedin"))
    assert s.is_seen_exact(j(url="https://boards.greenhouse.io/acme/jobs/123/"))
    assert not s.is_seen_exact(j(url="https://boards.greenhouse.io/acme/jobs/999"))


def test_fuzzy_dedupe_company_title(tmp_path):
    s = State(tmp_path / "s.db")
    s.record(j(company="McLaren Racing", title="Software Engineer Industrial Placement",
               url="https://a.com/1"), "bank")
    assert s.is_seen_fuzzy(j(company="McLaren Racing Ltd",
                             title="Industrial Placement - Software Engineer",
                             url="https://b.com/2"), threshold=92)
    assert not s.is_seen_fuzzy(j(company="Williams", title="Aerodynamics Placement",
                                 url="https://c.com/3"), threshold=92)


def test_volume_gate_cap_and_priority():
    """Priority roles are flagged and sort first, but they now COUNT against the
    slots rather than arriving as free extras on top — a bank that can be pushed
    past its size by high scorers is how 10 became 31. What priority still buys
    is entry when the slots are used up but the bank has headroom (below)."""
    jobs = [j(url=f"https://x.com/{i}", score=70 + (i % 10)) for i in range(30)]
    jobs.append(j(url="https://x.com/p1", score=90))   # priority
    jobs.append(j(url="https://x.com/p2", score=88))   # priority
    jobs.append(j(url="https://x.com/low", score=40))  # below min
    gate = volume_gate(jobs, slots=20, min_score=60, priority_score=85)
    assert len(gate.bank) == 20
    assert sum(job.priority for job in gate.bank) == 2   # both made the cut, ranked first
    assert len(gate.archive) == 13                       # 12 over cap + 1 below min
    banked_nonpriority = [job.score for job in gate.bank if not job.priority]
    assert min(banked_nonpriority) >= max(job.score for job in gate.archive
                                          if (job.score or 0) >= 60)


def test_role_signature_collapses_variants():
    def sig(title, company="Google"):
        return Job(source="t", company=company, title=title, url="https://x").role_signature
    assert sig("Software Engineering Intern - MS - Summer 2027") == \
        sig("Software Engineering Intern, BS, Summer 2027")
    # different role family stays distinct
    assert sig("Software Engineering Intern") != sig("Software Developer Intern")
    # different company stays distinct
    assert sig("SWE Intern", "Google") != sig("SWE Intern", "Meta")


def test_below_min_archived_silently():
    gate = volume_gate([j(score=59)], slots=20, min_score=60, priority_score=85)
    assert not gate.bank and len(gate.archive) == 1


def test_weekly_cap_is_rolling_not_per_run(tmp_path):
    """Daily runs must not multiply the weekly cap."""
    s = State(tmp_path / "s.db")
    for i in range(6):
        job = j(url=f"https://x.com/b{i}", score=80)
        s.record(job, "bank")
    assert s.banked_since(7) == 6
    # a cap of 10 with 6 already banked this week leaves 4 slots
    remaining = max(0, 10 - s.banked_since(7))
    assert remaining == 4
    gate = volume_gate([j(url=f"https://y.com/{i}", score=70) for i in range(20)],
                       slots=remaining, min_score=60, priority_score=85)
    assert len(gate.bank) == 4
    assert len(gate.archive) == 16


def test_only_llm_scored_jobs_can_enter_the_bank():
    """Two-stage scoring: cheap heuristic pre-rank must not push unreviewed roles
    into the Job Bank, even if their heuristic score clears MIN_SCORE."""
    reviewed = j(url="https://x.com/llm", score=70)
    reviewed.extra["llm_scored"] = True
    cheap = j(url="https://x.com/cheap", score=95)      # high but never LLM-reviewed
    cheap.extra["llm_scored"] = False
    gate = volume_gate([reviewed, cheap], slots=10, min_score=66, priority_score=85)
    assert [x.url for x in gate.bank] == ["https://x.com/llm"]
    assert [x.url for x in gate.archive] == ["https://x.com/cheap"]


# --- bank sizing: the cap is the SIZE of the bank, not the rate of arrivals ---

def test_every_run_offers_whatever_space_is_left():
    """No schedule and no refill threshold: the space under the ceiling is offered
    every morning, and the score gate decides whether anything deserves it."""
    assert open_slots(10, 10) == 0        # full: nothing to offer
    assert open_slots(9, 10) == 1         # one gap, one slot — same day, no waiting
    assert open_slots(4, 10) == 6
    assert open_slots(0, 10) == 10


def test_an_empty_slot_is_not_an_obligation_to_fill_it():
    """Space alone adds nothing: everything below min_score is archived, so a
    quiet run leaves the bank exactly as it was."""
    weak = [j(url=f"https://x.com/{i}", score=60) for i in range(5)]
    gate = volume_gate(weak, slots=10, min_score=75, priority_score=85, headroom=10)
    assert not gate.bank and len(gate.archive) == 5


def test_an_ordinary_priority_role_does_not_breach_the_ceiling():
    """Only a role that is ALSO closing this week gets the overflow (below)."""
    jobs = [j(url=f"https://x.com/{i}", score=95) for i in range(10)]   # all priority
    gate = volume_gate(jobs, slots=0, min_score=75, priority_score=85, headroom=2)
    assert len(gate.bank) == 2 and len(gate.archive) == 8


def test_priority_role_may_take_a_free_slot_above_the_refill_floor():
    """Bank of 7 (no refill due), one free slot's worth of headroom: an ordinary
    role waits, an exceptional one does not."""
    ordinary = j(url="https://x.com/ord", score=80)
    exceptional = j(url="https://x.com/exc", score=92)
    gate = volume_gate([ordinary, exceptional], slots=0, min_score=75,
                       priority_score=85, headroom=3)
    assert [x.url for x in gate.bank] == ["https://x.com/exc"]


def test_one_company_cannot_fill_the_bank():
    jobs = [j(company="TikTok", url=f"https://x.com/tt{i}", score=80) for i in range(9)]
    jobs.append(j(company="Figma", url="https://x.com/fig", score=78))
    gate = volume_gate(jobs, slots=10, min_score=75, priority_score=85,
                       headroom=10, max_per_company=3)
    assert len(gate.bank) == 4
    assert sum(1 for x in gate.bank if x.company == "TikTok") == 3
    assert "https://x.com/fig" in [x.url for x in gate.bank]


def test_company_cap_counts_what_the_bank_already_holds():
    jobs = [j(company="TikTok", url=f"https://x.com/tt{i}", score=80) for i in range(3)]
    gate = volume_gate(jobs, slots=10, min_score=75, priority_score=85, headroom=10,
                       max_per_company=3, company_counts={"tiktok": 2})
    assert len(gate.bank) == 1 and len(gate.archive) == 2


def test_bytedance_and_tiktok_count_as_one_employer():
    assert company_key("ByteDance") == company_key("TikTok")
    assert company_key("Renault Group") == company_key("Renault")
    assert company_key("Mistral Ai") != company_key("TikTok")


# --- the ceiling is a target, not a wall ---

def test_a_role_closing_this_week_may_go_past_the_ceiling():
    urgent = j(url="https://x.com/u", score=80)
    urgent.extra["urgent"] = True
    ordinary = j(url="https://x.com/o", score=80)
    gate = volume_gate([ordinary, urgent], slots=0, min_score=75, priority_score=85,
                       headroom=0, overflow=2)
    assert [x.url for x in gate.bank] == ["https://x.com/u"]
    assert [x.url for x in gate.archive] == ["https://x.com/o"]


def test_the_overflow_is_bounded():
    """"Not a hard number" is not "no number" — three urgent roles, room for two."""
    jobs = []
    for i in range(3):
        job = j(url=f"https://x.com/u{i}", score=80)
        job.extra["urgent"] = True
        jobs.append(job)
    gate = volume_gate(jobs, slots=0, min_score=75, priority_score=85,
                       headroom=0, overflow=2)
    assert len(gate.bank) == 2 and len(gate.archive) == 1


def test_an_ordinary_role_is_still_held_to_the_ceiling():
    jobs = [j(url=f"https://x.com/{i}", score=80) for i in range(4)]
    gate = volume_gate(jobs, slots=4, min_score=75, priority_score=85,
                       headroom=2, overflow=5)
    assert len(gate.bank) == 2
