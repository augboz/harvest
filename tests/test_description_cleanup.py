"""What a Job Bank description is allowed to look like.

Every one of these came off the real sheet: markdown scaffolding from GitHub
board mirrors and Ashby, and listing metadata from aggregator pages that repeats
the company, title and location the card already shows.
"""
from jobbank.backfill import _demarkdown, _reads_as_prose, _strip_listing_lead


def test_markdown_scaffolding_is_removed():
    assert _demarkdown(
        "**Company** ----------- Figma **Job Description** ------------------- "
        "We're looking for software engineering interns."
    ) == "We're looking for software engineering interns."


def test_a_french_posting_keeps_its_text():
    out = _demarkdown("**Job Description** ------- **Contexte :** Renault Group poursuit.")
    assert out.startswith("Contexte")


def test_plain_prose_is_left_alone():
    prose = "We are looking for interns. In this role you will ship features."
    assert _demarkdown(prose) == prose


def test_listing_metadata_is_stripped_back_to_the_sentence():
    raw = ("Mistral · 3 weeks ago AI Data Solutions Intern – Data Partnerships "
           "Palo Alto, CA Internship Onsite Intern Mistral is a company that "
           "provides full-stack AI solutions.")
    assert _strip_listing_lead(raw, "Mistral", "AI Data Solutions Intern – Data Partnerships",
                               "USA") == "Mistral is a company that provides full-stack AI solutions."


def test_the_company_name_survives_when_it_is_the_subject():
    """The company is stripped as a listing header but kept as a sentence subject —
    "is a company that provides…" would be worse than the metadata."""
    out = _strip_listing_lead("Exa Exa is an applied AI lab building a search engine.",
                              "Exa", "Research Intern", "USA")
    assert out == "Exa is an applied AI lab building a search engine."


def test_a_place_at_the_start_of_real_prose_is_kept():
    prose = "London, UK is where our studio sits and interns join the core team."
    assert _strip_listing_lead(prose, "Epic", "Intern", "London") == prose


def test_prose_detection_needs_lowercase_words():
    assert _reads_as_prose("we are looking for an intern to join the team today")
    assert not _reads_as_prose("AI Data Solutions Intern Data Partnerships Palo Alto CA Onsite")


def test_escaped_html_becomes_words():
    """One Archive row rendered as `class=&amp;quot;content-intro&amp;quot;...`
    on the card — HTML that was escaped twice on the way in. It also gave the
    More roles grid a 92-character unbreakable token, which stretched its column."""
    raw = ("About Anthropic class=&amp;quot;content-intro&amp;quot;&amp;gt;"
           "&amp;lt;h2&amp;gt;&amp;lt;strong&amp;gt;About Anthropic"
           "&amp;lt;/strong&amp;gt;&amp;lt;/h2&amp;gt; We build reliable AI systems.")
    out = _demarkdown(raw)
    assert "&" not in out and "<" not in out and "class=" not in out
    assert out.endswith("We build reliable AI systems.")


def test_an_equals_sign_in_prose_is_not_markup():
    prose = "We use x=1 in our code and build tools for engineers today."
    assert _demarkdown(prose) == prose


def test_a_rule_of_equals_signs_is_not_a_word():
    """A 114-character run of "=" is what actually widened the column."""
    assert _demarkdown("Role " + "=" * 114 + " We are hiring interns.") == \
        "We are hiring interns."
