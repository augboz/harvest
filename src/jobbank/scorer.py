"""Stage 2 scoring (spec sec 4): batched LLM scoring of hard-filter survivors,
strict JSON out. Falls back to a clearly-labelled heuristic when no
ANTHROPIC_API_KEY is configured so runs still complete end-to-end.
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta

from .models import Job, matches_watchlist
from .settings import Settings

CATEGORIES = ["AI/ML", "SWE", "MusicTech", "SportsData", "Motorsport", "Space",
              "BigTech", "Other"]

SYSTEM_PROMPT = """You score internship/placement postings 1-100 for one specific candidate.

CANDIDATE: Augustin ("Aug"), MEng Computer Science at Imperial College London (Sept 2024 -
June 2028), London-based, penultimate-year candidate for 2027 roles (third-year from Oct 2026).
French AND British citizen: full UK+EU work rights; US roles need sponsorship (hard for
international students). Fluent English and French.
Experience: SWE intern at DraftKings London (sports data platform, summer 2026); Pharma
Sentinel (Python data pipelines, 2023); Arquant Capital Paris (2022) - NOTE: that was a crypto
fund but must NOT be read as quant/trading interest; quant roles are excluded upstream.
Projects: Bozz desktop productivity app (TypeScript/React/Tauri/Supabase, 50+ releases);
F1 Telemetry Analyser (FastF1); ARMv8 emulator/assembler in C; Sharify (Spotify API);
AI Chef. Open-source contributions to beets and mutagen. Skills: Python, C, TypeScript/React,
Java/Kotlin, SQL, Rust/Tauri, PyTorch, data pipelines, ARM assembly/embedded, CI/CD, OAuth2/APIs.
Signals: Imperial MEng, IB 42/45 Valedictorian, DraftKings internship secured - a strong
candidate for competitive schemes, but an undergraduate with NO ML research publications.

INTERESTS - ALL EQUAL WEIGHT, no priority order: AI/ML; software engineering (apps, backend,
platforms, data systems); music & audio tech; sports & sports data; F1/motorsport/cars/
automotive tech; space; big tech; entertainment/media tech. A year in industry with an F1 team
is a dream outcome and should be treated as such.
He actively likes STARTUPS in the world's major hubs — London, Paris, Zurich/Geneva, Berlin,
Amsterdam, Toronto, Singapore, NYC/SF. An internship at a strong watchlist startup (AI labs,
dev tools, music tech, sports data) deserves the same company-appeal credit as a big name;
never mark a role down just because the company is small.

SCORING RUBRIC (weights sum to 100):
- Role/interest match (30): all interest areas equal. F1 team year-in-industry roles get the
  top of this band.
- Attainability (20): how well his ACTUAL profile matches the stated requirements (skills,
  degree stage, experience level); competitiveness of the programme (a 20-place big-tech
  scheme scores lower here than a mid-size firm wanting exactly his stack); any eligibility
  friction (e.g. US sponsorship). Be honest in the reason.
- Duration & timing fit (20): 6-month Apr-Sep 2027 = full; 12-month 2027-28 = near-full;
  Summer 2027 internship = roughly half.
- Company appeal (15): watchlist companies and clear passion-area employers highest;
  unknown agencies/bodyshops low.
- Location & work rights (10): UK + France/EU = full; remote high. Elsewhere (incl. the
  US) = 6-8, NOT a knockout. Location is a mild convenience preference, never a veto: a
  genuinely excellent role at a passion-area company (say Spotify New York, or an F1 team
  anywhere) MUST still score highly and reach the shortlist on merit. Mention any visa/
  sponsorship friction in the attainability note instead of suppressing the score.
  (The only geography that IS excluded is US ITAR-restricted defence/launch employers,
  which legally cannot hire non-US persons — those are filtered out upstream.)
- Freshness & deadline (5): if closing imminently, flag URGENT in the reason - do not
  score it down for that.

CATEGORIES (pick exactly one): AI/ML, SWE, MusicTech, SportsData, Motorsport, Space,
BigTech, Other.

OUTPUT: a JSON array only, no prose, one object per input job, same order, schema:
{"id": <input id>, "score": <int 1-100>, "reason": "<=40 words, honest, mention URGENT if deadline imminent",
 "category": "<one of the categories>", "attainability_note": "<=25 words on interview/offer likelihood",
 "deadline": "<YYYY-MM-DD if the text states an application deadline/closing date, else null>",
 "duration": "<how long the role lasts, e.g. '12 months' / '6 months' / '10-12 weeks', else null>",
 "start": "<when it starts, e.g. 'Summer 2027' / 'September 2027' / 'Apr 2027', else null>"}

Do NOT write a summary of the role. The sheet shows the opening lines of the advert
itself, which is cheaper and more honest than a paraphrase.

Duration and start are almost always stated somewhere in the posting - extract them
rather than returning null unless the text genuinely never says."""

WATCHLIST_APPEAL_HINT = "This company is on the candidate's target watchlist."


# A 400 from the Messages API is a configuration or billing fact, not a blip:
# a spent credit balance, a rejected key or an unknown model id fails identically
# on every remaining batch. Retrying 50 jobs' worth of batches twice each against
# a known-dead account wastes a minute and buries the one line that matters.
def _llm_unavailable_note(exc) -> str:
    """A short, honest reason for the sheet. The full error goes to the log."""
    msg = str(getattr(exc, "message", "") or exc)
    low = msg.lower()
    if "credit balance" in low:
        return "(LLM unavailable: Anthropic credit balance too low)"
    if "api key" in low or "authentication" in low or "x-api-key" in low:
        return "(LLM unavailable: API key rejected)"
    if "model" in low:
        return "(LLM unavailable: model not accepted)"
    return "(LLM unavailable: " + " ".join(msg.split())[:70] + ")"


def _job_payload(i: int, job: Job, watchlist_names: set[str],
                 desc_chars: int = 1200) -> dict:
    return {
        "id": i,
        "company": job.company,
        "title": job.title,
        "location": job.location,
        "posted": job.posted.isoformat() if job.posted else None,
        "deadline": job.deadline.isoformat() if job.deadline else None,
        "duration": job.duration or None,
        "start": job.start or None,
        "salary": job.salary or None,
        "source": job.source,
        "watchlist": matches_watchlist(job.company, watchlist_names) or None,
        "description": (job.description or "")[:desc_chars],
    }


def _parse_batch(text: str, batch: list[Job]) -> bool:
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return False
    try:
        rows = json.loads(m.group(0))
    except json.JSONDecodeError:
        return False
    by_id = {r.get("id"): r for r in rows if isinstance(r, dict)}
    ok = 0
    for i, job in enumerate(batch):
        r = by_id.get(i)
        if not r or not isinstance(r.get("score"), (int, float)):
            continue
        job.score = max(1, min(100, int(r["score"])))
        job.score_reason = str(r.get("reason", ""))[:400]
        cat = str(r.get("category", "Other"))
        job.category = cat if cat in CATEGORIES else "Other"
        job.attainability_note = str(r.get("attainability_note", ""))[:250]
        # deadlines are usually stated only in the description text, so take the
        # LLM's extraction when the source didn't give us a structured one
        if not job.deadline and r.get("deadline"):
            try:
                from datetime import date as _date
                job.deadline = _date.fromisoformat(str(r["deadline"])[:10])
            except (ValueError, TypeError):
                pass
        # duration/start are stated in prose the regex enricher often misses
        if r.get("duration"):
            job.duration = str(r["duration"])[:40]
        if r.get("start"):
            job.start = str(r["start"])[:40]
        ok += 1
    return ok == len(batch)


def score_jobs(jobs: list[Job], settings: Settings) -> dict:
    """Two-stage scoring, in place.

    Stage 1 (free): heuristic-score EVERY survivor.
    Stage 2 (paid): re-score only the top `llm_top_n` by heuristic with the LLM.

    Only LLM-scored jobs are eligible for the Job Bank (see `llm_scored`), so the
    cheap pre-rank can never push an unreviewed role in front of Aug. Keeps a run
    to a few cents instead of scoring hundreds of jobs at full price.
    """
    watchlist_names = {c["name"].lower() for c in settings.watchlist}
    api_key = settings.env("ANTHROPIC_API_KEY")
    cfg = settings.cfg["scorer"]
    jobs = jobs[: cfg.get("max_jobs_per_run", 400)]

    # Stage 1: free heuristic pass over everything
    for job in jobs:
        heuristic_score(job, watchlist_names)
    if not api_key:
        return {"mode": "heuristic", "llm_calls": 0, "est_cost_usd": 0.0,
                "llm_scored": 0, "prefiltered": len(jobs)}

    # Stage 2: LLM only for the most promising slice.
    # Watchlist companies ALWAYS get LLM review regardless of heuristic rank — the
    # cheap pre-rank can't read a JD and was demoting real targets (it dropped
    # Mistral AI out of the top 50). Cheap insurance against a false negative.
    top_n = int(cfg.get("llm_top_n", 50))
    ranked = sorted(jobs, key=lambda j: j.score or 0, reverse=True)
    shortlist = ranked[:top_n]
    chosen = {id(j) for j in shortlist}
    forced = [j for j in ranked
              if matches_watchlist(j.company, watchlist_names) and id(j) not in chosen]
    if forced:
        print(f"  [scorer] +{len(forced)} watchlist role(s) forced into LLM review")
        shortlist = shortlist + forced
    for job in shortlist:
        job.score = None          # let the LLM set the real score
        job.extra["llm_candidate"] = True
    print(f"  [scorer] heuristic pre-rank of {len(jobs)} -> LLM scoring top {len(shortlist)}")

    # Candidate feedback is appended rather than baked into SYSTEM_PROMPT so the
    # rubric stays the stable, reviewable part and the learned bit is visibly
    # separate (and empty on a fresh profile).
    from .feedback import digest_for
    system_prompt = SYSTEM_PROMPT + digest_for(settings)

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    batch_size = cfg.get("batch_size", 12)
    desc_chars = int(cfg.get("description_chars", 1200))
    calls, in_tok, out_tok = 0, 0, 0
    fatal = ""          # set once a failure is known to repeat on every batch
    scored_ok = 0
    for start in range(0, len(shortlist), batch_size):
        if fatal:
            break
        batch = shortlist[start:start + batch_size]
        payload = json.dumps([_job_payload(i, j, watchlist_names, desc_chars)
                              for i, j in enumerate(batch)], ensure_ascii=False)
        parsed = False
        for _attempt in range(2):
            try:
                resp = client.messages.create(
                    model=cfg["model"],
                    max_tokens=int(cfg.get("max_tokens", 8000)),
                    system=system_prompt,
                    messages=[{"role": "user", "content": f"Score these jobs:\n{payload}"}])
                calls += 1
                in_tok += resp.usage.input_tokens
                out_tok += resp.usage.output_tokens
                if _parse_batch(resp.content[0].text, batch):
                    parsed = True
                    break
                # a truncated response is the usual cause and is invisible
                # otherwise: the JSON simply stops mid-object and fails to parse
                print(f"  [scorer] batch {start // batch_size} unparsed "
                      f"(stop_reason={resp.stop_reason}, "
                      f"out={resp.usage.output_tokens} tok)")
            except (anthropic.AuthenticationError, anthropic.PermissionDeniedError,
                    anthropic.NotFoundError, anthropic.BadRequestError) as exc:
                # billing / key / model — identical on every remaining batch
                print(f"  [scorer] LLM unavailable: {exc}")
                fatal = _llm_unavailable_note(exc)
                break
            except anthropic.APIError as exc:      # rate limit, 5xx, network
                print(f"  [scorer] API error on batch {start//batch_size}: {exc}")
        if parsed:
            scored_ok += len(batch)
        else:
            for job in batch:
                if job.score is None:
                    heuristic_score(job, watchlist_names,
                                    note=fatal or "(LLM batch failed)")
                job.extra["llm_failed"] = True
    # Breaking out on a fatal error leaves the batches never reached still holding
    # score=None from the shortlist reset above — they would land on the sheet with
    # no score at all. Give them the same fallback the attempted batches got.
    for job in shortlist:
        if job.score is None:
            heuristic_score(job, watchlist_names, note=fatal or "(LLM batch failed)")
            job.extra["llm_failed"] = True

    for job in jobs:
        # This read `bool(llm_candidate)` — true for every SHORTLISTED job
        # whether or not the LLM ever ran. On 2026-08-25, with the credit balance
        # spent, that let unreviewed heuristic scores walk straight into the bank
        # and quietly inverted the promise in this function's docstring.
        job.extra["llm_scored"] = (bool(job.extra.get("llm_candidate"))
                                   and not job.extra.get("llm_failed"))
    # Haiku 4.5 pricing: $1/MTok in, $5/MTok out
    cost = in_tok / 1e6 * 1.0 + out_tok / 1e6 * 5.0
    if fatal or not scored_ok:
        print(f"  [scorer] !! NOTHING WAS LLM-SCORED {fatal} — every score this run "
              f"is the heuristic fallback, and none of it is bank-eligible")
    return {"mode": "llm" if scored_ok else "heuristic (LLM unavailable)",
            "llm_calls": calls, "est_cost_usd": round(cost, 4),
            "llm_scored": scored_ok, "prefiltered": len(jobs) - len(shortlist),
            "llm_error": fatal}


# ---------------------------------------------------------------------------
INTEREST_MAP = [
    ("Motorsport", r"\b(f1|formula ?(1|one|e)|motorsport|racing|automotive|ferrari|mclaren|"
                   r"mercedes|red bull|williams|aston martin|alpine|sauber|audi|porsche|cosworth)\b"),
    ("MusicTech", r"\b(audio|music|dsp|spotify|ableton|serato|native instruments|focusrite|dj)\b"),
    ("SportsData", r"\b(sports?|draftkings|genius sports|stats perform|betting|fanduel)\b"),
    ("Space", r"\b(space|satellite|esa|airbus|thales|orbital|spacecraft)\b"),
    ("AI/ML", r"\b(machine learning|ml|ai|artificial intelligence|deep learning|llm|"
              r"computer vision|nlp|data scien)\b"),
    ("BigTech", r"\b(google|meta|apple|amazon|microsoft|nvidia|netflix|sky|tiktok|"
                      r"bytedance|snap|bloomberg|media)\b"),
    ("SWE", r"\b(software|developer|backend|frontend|full[- ]stack|platform|data|cloud|devops)\b"),
]


def heuristic_score(job: Job, watchlist_names: set[str], note: str = "") -> None:
    """Deterministic fallback used only when the LLM is unavailable.
    Clearly labelled in the reason so nothing masquerades as real scoring."""
    hay = job.haystack
    category, interest = "Other", 10
    for cat, pattern in INTEREST_MAP:
        if re.search(pattern, hay, re.I):
            category = cat
            interest = 26 if cat == "Motorsport" else 22
            break
    attain = 12  # unknown requirements -> middling
    timing = 20 if re.search(r"12[- ]month|year in industry|industrial placement|placement year", hay) \
        else 16 if re.search(r"6[- ]month|apr|april", hay) \
        else 14 if "2027" in hay \
        else 12 if re.search(r"intern|placement|stage|co[- ]?op", hay) \
        else 8   # a plain internship posted now is almost always the 2027 cycle
    agency_re = re.compile(r"recruit|staffing|resourc|talent|solutions|consult|"
                           r"agency|personnel|jobs? ?board|hays|reed|adecco", re.I)
    if matches_watchlist(job.company, watchlist_names):
        appeal = 15
    elif agency_re.search(job.company):
        appeal = 2   # bodyshop / agency listing
    else:
        appeal = 7
    loc = job.location.lower()
    # Location is a mild convenience preference, NOT a veto: a genuinely great role
    # (e.g. Spotify New York) must still be able to reach the bank on merit.
    # Visa friction belongs in the attainability note, not in a big score penalty.
    location = 10 if any(k in loc for k in ("london", "united kingdom", "uk", "france", "paris",
                                            "remote")) \
        else 9 if any(k in loc for k in ("germany", "netherlands", "italy", "spain", "europe",
                                         "ireland", "switzerland", "belgium", "sweden")) \
        else 7  # elsewhere incl. US: slight nudge, not a knockout
    fresh = 4
    # heuristic can't read a JD, so lean on the profile's watchlist: a known
    # passion-area employer should clearly outrank an unknown agency posting.
    watchlist_bonus = 6 if matches_watchlist(job.company, watchlist_names) else 0
    job.score = min(100, interest + attain + timing + appeal + location + fresh
                    + watchlist_bonus)
    job.category = category
    urgent = ""
    if job.deadline and job.deadline <= date.today() + timedelta(days=7):
        urgent = "URGENT deadline. "
    job.score_reason = (f"{urgent}HEURISTIC score {note or '(no LLM key)'}: interest={interest} "
                        f"timing={timing} appeal={appeal} location={location}").strip()
    job.attainability_note = "Heuristic mode - requirements not assessed by LLM."
