from datetime import date, timedelta

from jobbank.filters import FilterStats, apply_hard_filters
from jobbank.models import Job

TODAY = date(2026, 7, 15)


def j(**kw):
    base = dict(source="test", company="Acme", title="Software Engineer Intern 2027",
                url="https://x.com/1")
    base.update(kw)
    return Job(**base)


def run_one(job, freshness=45):
    passed, rejected = apply_hard_filters([job], freshness, FilterStats(), today=TODAY)
    return rejected[0][1] if rejected else None


# --- quant exclusion is absolute -------------------------------------------
def test_quant_title_rejected():
    assert "quant" in run_one(j(title="Quantitative Trading Intern"))

def test_quant_company_rejected():
    assert "quant" in run_one(j(company="Jane Street", title="Software Intern",
                                description="trading systems for market making"))

def test_hedge_fund_description_rejected():
    assert "quant" in run_one(j(description="We are a leading hedge fund."))


# --- type -------------------------------------------------------------------
def test_graduate_scheme_rejected():
    assert "type" in run_one(j(title="Graduate Scheme - Software Engineer"))

def test_permanent_rejected():
    assert run_one(j(title="Senior Software Engineer")) is not None

def test_placement_accepted():
    assert run_one(j(title="Software Industrial Placement 2027")) is None

def test_stage_accepted():
    assert run_one(j(title="Stage ingénieur logiciel 2027")) is None


# --- qualifications (strict) --------------------------------------------------
def test_phd_required_rejected():
    assert "qualifications" in run_one(j(
        description="Candidates must be pursuing a PhD in Computer Science."))

def test_research_scientist_rejected():
    assert "qualifications" in run_one(j(
        title="Research Scientist Intern",
        description="Machine learning research scientist internship."))

def test_masters_only_rejected():
    assert "qualifications" in run_one(j(
        description="Open to Master's students only."))

def test_years_experience_rejected():
    assert "qualifications" in run_one(j(
        title="Software Placement", description="Requires at least 3 years experience."))

def test_incompatible_graduation_rejected():
    assert "qualifications" in run_one(j(
        description="You must be graduating by December 2026."))

def test_phd_or_masters_accepted():
    # "Master's or PhD" welcomes Master's-level students -> not PhD-required
    assert run_one(j(title="AI Inference Internship",
                     description="Program for exceptional Master's or PhD students studying CS.")) is None

def test_phd_only_still_rejected():
    assert "qualifications" in run_one(j(
        description="This programme is for PhD students only, pursuing a doctorate."))

def test_2028_graduation_accepted():
    assert run_one(j(description="Graduating in 2028 or 2029.")) is None

def test_board_row_without_description_passes_qualifications():
    assert run_one(j(description="")) is None

def test_phd_in_title_rejected_even_without_description():
    assert "qualifications" in run_one(j(
        title="Research Scientist Intern - Computer Vision - PhD", description=""))


# --- timing -------------------------------------------------------------------
def test_summer_2026_rejected():
    assert "timing" in run_one(j(title="Software Engineer Intern - Summer 2026"))

def test_summer_2027_accepted():
    assert run_one(j(title="Software Engineer Intern - Summer 2027")) is None

def test_2028_only_rejected():
    assert "timing" in run_one(j(title="Software Intern", description="Summer 2028 cohort."))

def test_12_month_2027_28_accepted():
    assert run_one(j(description="12-month placement from summer 2027 to summer 2028.")) is None


# --- ITAR ---------------------------------------------------------------------
def test_spacex_rejected():
    assert "itar" in run_one(j(company="SpaceX", title="Software Engineer Intern"))

def test_us_person_text_rejected():
    assert "itar" in run_one(j(description="To conform to U.S. Government export "
                                           "regulations (ITAR), applicant must be a U.S. Person."))

def test_esa_accepted():
    assert run_one(j(company="ESA", title="Software Engineering Intern",
                     description="Young graduate trainee software, Noordwijk.")) is None


# --- field --------------------------------------------------------------------
def test_mechanical_rejected():
    assert "field" in run_one(j(title="Mechanical Engineering Intern"))

def test_marketing_rejected():
    assert "field" in run_one(j(title="Marketing Intern"))

def test_audio_software_accepted():
    assert run_one(j(title="Audio Software Intern")) is None


# --- freshness ------------------------------------------------------------------
def test_stale_posting_rejected():
    assert "freshness" in run_one(j(posted=TODAY - timedelta(days=60)))

def test_stale_with_future_deadline_kept():
    assert run_one(j(posted=TODAY - timedelta(days=60),
                     deadline=TODAY + timedelta(days=10))) is None

def test_passed_deadline_rejected():
    assert "freshness" in run_one(j(deadline=TODAY - timedelta(days=1)))


def test_advisor_titles_are_rejected_like_advisory_ones():
    """Auto Trader's "Early Careers Advisor - 12 month FTC" reached the bank:
    CONSULTING_RE had "advisory" but not "advisor", and the employer being a tech
    company meant the description still carried a tech signal."""
    from jobbank.filters import CONSULTING_RE
    for title in ("Early Careers Advisor - 12 month FTC",
                  "Strategic Customer Advisor",
                  "Technical Adviser, Digital"):
        assert CONSULTING_RE.search(title), title
    # ...without taking the early-careers ENGINEERING roles down with them
    for title in ("Early Careers Software Engineer",
                  "Graduate Software Engineer",
                  "Software Engineer Intern"):
        assert not CONSULTING_RE.search(title), title


# ------------------------------------------------------------ posting URLs ---
def test_a_search_page_is_not_a_posting():
    """Trackr handed the bank an Apple 'role' whose link was a search page; it
    LLM-scored 72 on its JS-shell description and sat there as if real."""
    from jobbank.models import is_posting_url
    assert not is_posting_url(
        "https://jobs.apple.com/en-gb/search?sort=relevance&key=internship")
    assert not is_posting_url("https://careers.dyson.com/en-gb/search-results/")
    assert not is_posting_url("https://www.mclaren.com/careers/")
    assert not is_posting_url("https://example.com")
    assert not is_posting_url("")


def test_a_specific_posting_url_always_passes():
    from jobbank.models import is_posting_url
    assert is_posting_url(
        "https://www.google.com/about/careers/applications/jobs/results/914361048")
    assert is_posting_url("https://boards.greenhouse.io/mclaren/jobs/712345")
    assert is_posting_url("https://jobs.lever.co/verkor/8a2c-11d4")
    assert is_posting_url("https://www.linkedin.com/jobs/view/3990123456")
    assert is_posting_url("https://apply.careers.microsoft.com/careers/job/197039")


def test_the_hard_filters_reject_landing_links():
    from jobbank import filters
    from jobbank.models import Job
    j = Job(source="trackr", company="Apple", title="Software Engineer Intern",
            url="https://jobs.apple.com/en-gb/search?key=internship")
    passed, rejected = filters.apply_hard_filters([j], 75)
    assert passed == [] and "landing/search page" in rejected[0][1]


# ---------------------------------------------------------- company rules ---
def _tt(location, company="TikTok"):
    from jobbank.models import Job
    return Job(source="t", company=company, title="Software Engineer Intern - 2027",
               url="https://careers.tiktok.com/position/712345/detail",
               location=location)


def test_tiktok_outside_europe_is_rejected_before_scoring():
    """Aug has ONE TikTok application left; a San Jose variant is a slot he
    cannot spend. 34 US variants were archived before this rule existed."""
    from jobbank.filters import apply_hard_filters
    rules = [{"company": "TikTok", "location_any": ["london", "paris", "europe"]}]
    passed, rejected = apply_hard_filters(
        [_tt("San Jose, USA"), _tt("London, UK")], 75, company_rules=rules)
    assert [j.location for j in passed] == ["London, UK"]
    assert "company rule" in rejected[0][1]


def test_company_rules_do_not_touch_other_companies():
    from jobbank.filters import apply_hard_filters
    rules = [{"company": "TikTok", "location_any": ["london"]}]
    passed, _ = apply_hard_filters(
        [_tt("San Jose, USA", company="Exa")], 75, company_rules=rules)
    assert len(passed) == 1


def test_no_rules_configured_is_a_no_op():
    from jobbank.filters import apply_hard_filters
    passed, _ = apply_hard_filters([_tt("San Jose, USA")], 75)
    assert len(passed) == 1
