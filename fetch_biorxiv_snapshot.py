#!/usr/bin/env python3
"""
fetch_biorxiv_snapshot.py

Pulls recent bioRxiv preprints, resolves the institutions involved against a
curated lookup table (institutions.json), and writes stories.json in the
shape the News Channel front end expects.

WHY THIS EXISTS
----------------
bioRxiv's main /details endpoint only gives you the CORRESPONDING author's
institution as a single free-text string -- not the full list of institutes
behind a paper. To get every institution (needed for the connecting-arc
feature), this script also fetches each paper's JATS XML (linked via the
`jatsxml` field) and pulls every <aff> (affiliation) block out of it. That's
a best-effort step: a handful of papers will fail to parse cleanly, and
that's fine -- they just fall back to the single corresponding-author
institution, or get dropped if even that doesn't resolve.

Institutions are matched against institutions.json by simple substring
matching, not a live geocoder. Anything that doesn't match gets logged to
unmatched_affiliations.txt instead of guessing at coordinates -- add real
entries there to institutions.json by hand as it grows. This keeps every pin
on the globe backed by a coordinate a person actually chose, rather than an
automated guess.

HOW STORIES GET SELECTED (AND WHY NOT "NEWSWORTHY")
------------------------------------------------------
bioRxiv posts hundreds of preprints a day across ~25 subject categories, far
more than a globe can show without turning into a wall of pins. Rather than
narrowing to a few hand-picked categories (which would stop being "general"),
this script pulls broadly across ALL categories and then ranks each paper by
how many distinct institutions collaborated on it -- more institutions means
a bigger undertaking, and it has the nice side effect of favoring exactly the
papers that get to show off the connecting-arc feature.

Deliberately NOT called "newsworthiness": a real importance signal (Altmetric
Attention Score, citations, journal acceptance) only exists for a paper once
enough time has passed for the wider world to react to it -- querying it the
same day a paper posts just returns near-zero for everything, good or bad
alike, because nothing has had time to happen yet. So this script optimizes
for a narrower, honest claim -- "how large a collaboration was this" -- which
is knowable on day one, rather than pretending to measure importance it
structurally can't see yet. If a delayed, attention-validated tier turns out
to be worth the extra machinery (an API key, a rolling history of candidates
to re-check weeks later), that's a natural v2 once the basic version has been
run against real data and you know whether the simple version over- or
under-curates.

To keep the result feeling general rather than dominated by whichever
category happened to post the most that week, selection also enforces a soft
per-category cap (see --category-cap-fraction) so no single subject can eat
the whole snapshot.

USAGE
-----
    python3 fetch_biorxiv_snapshot.py --days 3 --pool-size 200 --max-stories 30

Run this on a schedule (cron, GitHub Actions, etc.) and commit/publish the
resulting stories.json alongside the static site. The site just fetches
stories.json at load time -- no API key ever touches the browser.

NOTE ON TESTING
----------------
This was written and reviewed carefully against bioRxiv's documented API
shape, but I couldn't actually execute it against api.biorxiv.org from this
environment (it's not on the sandbox's allowed network list). Run it
yourself and sanity-check the first output before wiring it into a schedule.
"""

import argparse
import json
import re
import sys
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, timezone

BIORXIV_DETAILS = "https://api.biorxiv.org/details/biorxiv/{interval}/{cursor}"
REQUEST_TIMEOUT = 20
REQUEST_DELAY = 0.4  # be polite between requests, even though bioRxiv has no stated rate limit

HERE = Path(__file__).parent
INSTITUTIONS_PATH = HERE / "institutions.json"
OUTPUT_PATH = HERE / "stories.json"
UNMATCHED_LOG_PATH = HERE / "unmatched_affiliations.txt"


def http_get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "wii-news-globe-snapshot/1.0"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_get_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": "wii-news-globe-snapshot/1.0"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fetch_recent_papers(days, pool_size):
    """Page through the bioRxiv /details endpoint for the last `days` days,
    gathering a broad candidate pool (before any collaboration-scale ranking).

    Uses an explicit YYYY-MM-DD/YYYY-MM-DD date range rather than bioRxiv's
    documented "Nd" (most recent N days) shorthand -- the shorthand is in
    their docs, but in practice returned an empty collection with no error
    on a real run, while every real-world example of this API (bioRxiv's own
    worked example, third-party R/Python wrappers, blog posts) uses explicit
    date ranges instead. Explicit dates are the well-trodden, verified path.
    """
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days)
    interval = f"{start.isoformat()}/{today.isoformat()}"

    papers = []
    cursor = 0
    first_page = True
    while len(papers) < pool_size:
        url = BIORXIV_DETAILS.format(interval=interval, cursor=cursor)
        try:
            data = http_get_json(url)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            print(f"  ! failed to fetch {url}: {e}", file=sys.stderr)
            break

        batch = data.get("collection", [])
        if not batch:
            if first_page:
                # Surface *why* it's empty instead of silently writing zero
                # stories -- the 'messages' field usually explains it (bad
                # date range, no posts in range, API-side issue, etc).
                print(f"  ! no results for {url}", file=sys.stderr)
                print(f"    messages: {data.get('messages')}", file=sys.stderr)
            break
        papers.extend(batch)
        first_page = False

        messages = data.get("messages", [{}])
        total = messages[0].get("total", len(batch)) if messages else len(batch)
        cursor += len(batch)
        if cursor >= int(total):
            break
        time.sleep(REQUEST_DELAY)

    return papers[:pool_size]


def extract_affiliations_from_jats(xml_text):
    """Pull every distinct <aff> block's text out of a JATS XML document.
    Best-effort: JATS structure varies enough between publishers/versions
    that we just grab all affiliation text rather than trying to bind each
    one to a specific author -- we only need the set of institutions."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    affiliations = []
    for aff in root.iter():
        tag = aff.tag.split("}")[-1]  # strip namespace
        if tag == "aff":
            text = "".join(aff.itertext())
            text = re.sub(r"\s+", " ", text).strip()
            if text:
                affiliations.append(text)
    return affiliations


def load_institutions():
    with open(INSTITUTIONS_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    raw.pop("_comment", None)
    return raw


def match_institution(affiliation_text, institutions):
    """Return the institution key if any match_key is a substring of the
    (lowercased) affiliation text, else None."""
    lowered = f" {affiliation_text.lower()} "
    for key, entry in institutions.items():
        for pattern in entry.get("match_keys", []):
            if pattern in lowered:
                return key
    return None


def resolve_story_institutions(paper, institutions, unmatched_log):
    """Best-effort: try the full JATS affiliation list first, fall back to
    just the corresponding author's institution."""
    raw_affils = []

    jats_url = paper.get("jatsxml")
    if jats_url:
        try:
            xml_text = http_get_text(jats_url)
            raw_affils = extract_affiliations_from_jats(xml_text)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            print(f"    (jats fetch failed for {paper.get('doi')}: {e})", file=sys.stderr)
        time.sleep(REQUEST_DELAY)

    if not raw_affils:
        corresponding = paper.get("author_corresponding_institution", "").strip()
        if corresponding:
            raw_affils = [corresponding]

    matched_keys = []
    for text in raw_affils:
        key = match_institution(text, institutions)
        if key and key not in matched_keys:
            matched_keys.append(key)
        elif not key:
            unmatched_log.add(text)

    return matched_keys


def build_story(paper, inst_keys, story_id):
    abstract = paper.get("abstract", "").strip()
    snippet = abstract[:320] + ("…" if len(abstract) > 320 else "")
    return {
        "id": story_id,
        "cat": "science",  # bioRxiv's own subject categories could map to finer buckets later
        "subject": paper.get("category", ""),
        "title": paper.get("title", "").strip(),
        "body": snippet,
        "inst": inst_keys,
        "doi": paper.get("doi", ""),
        "date": paper.get("date", ""),
    }


def collaboration_score(candidate):
    """Free, no-extra-API proxy for how large an undertaking a paper was:
    more distinct collaborating institutions. This is NOT a newsworthiness
    or importance signal -- there isn't a free one available same-day (see
    module docstring). It's honestly just "how many places worked on this,"
    which happens to also be a fine thing to select for on its own, and
    guarantees the pins shown come with arcs to draw."""
    return len(candidate["inst_keys"])


def select_stories(candidates, max_stories, category_cap_fraction):
    """Greedily pick the highest-collaboration-scale candidates, but cap how
    many can come from any single bioRxiv subject category so the result
    stays general rather than dominated by whichever category posted the
    most."""
    per_category_cap = max(1, round(max_stories * category_cap_fraction))
    candidates_sorted = sorted(
        candidates,
        key=lambda c: (collaboration_score(c), c["paper"].get("date", "")),
        reverse=True,
    )

    selected = []
    category_counts = {}
    leftovers = []
    for c in candidates_sorted:
        category = c["paper"].get("category", "")
        if category_counts.get(category, 0) >= per_category_cap:
            leftovers.append(c)
            continue
        selected.append(c)
        category_counts[category] = category_counts.get(category, 0) + 1
        if len(selected) >= max_stories:
            return selected

    # under-filled categories: top up from leftovers (still score-ordered)
    for c in leftovers:
        if len(selected) >= max_stories:
            break
        selected.append(c)

    return selected


def main():
    parser = argparse.ArgumentParser(description="Fetch a bioRxiv snapshot for the News Channel globe.")
    parser.add_argument("--days", type=int, default=3, help="How many recent days of preprints to pull.")
    parser.add_argument("--pool-size", type=int, default=150, help="Size of the candidate pool considered before scoring.")
    parser.add_argument("--max-stories", type=int, default=30, help="How many stories to actually publish.")
    parser.add_argument("--category-cap-fraction", type=float, default=0.25,
                         help="Max share of published stories that can come from one bioRxiv subject category.")
    args = parser.parse_args()

    print(f"Fetching last {args.days} day(s) of bioRxiv preprints (pool size {args.pool_size})...")
    papers = fetch_recent_papers(args.days, args.pool_size)
    print(f"  got {len(papers)} candidate papers")

    institutions = load_institutions()
    unmatched_log = set()
    if UNMATCHED_LOG_PATH.exists():
        unmatched_log.update(
            line.strip() for line in UNMATCHED_LOG_PATH.read_text(encoding="utf-8").splitlines() if line.strip()
        )

    print("Resolving institutions for each candidate (this is the slow part)...")
    candidates = []
    for i, paper in enumerate(papers):
        title = paper.get("title", "(untitled)")
        print(f"  [{i+1}/{len(papers)}] {title[:70]}")
        inst_keys = resolve_story_institutions(paper, institutions, unmatched_log)
        if not inst_keys:
            print("      -> no institutions resolved, dropping from candidate pool")
            continue
        candidates.append({"paper": paper, "inst_keys": inst_keys})

    print(f"\n{len(candidates)} candidates have at least one resolved institution.")
    selected = select_stories(candidates, args.max_stories, args.category_cap_fraction)
    print(f"Selected {len(selected)} stories (cap: {args.max_stories}, "
          f"~{args.category_cap_fraction:.0%} max per category).")

    stories = []
    used_institution_keys = set()
    for c in selected:
        used_institution_keys.update(c["inst_keys"])
        stories.append(build_story(c["paper"], c["inst_keys"], story_id=len(stories)))

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "institutions": {k: institutions[k] for k in used_institution_keys},
        "stories": stories,
    }

    OUTPUT_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote {len(stories)} stories -> {OUTPUT_PATH}")

    # Always write this file, even empty -- a missing file (rather than an
    # empty one) is what broke `git add unmatched_affiliations.txt` in CI
    # when nothing was unmatched.
    UNMATCHED_LOG_PATH.write_text(
        ("\n".join(sorted(unmatched_log)) + "\n") if unmatched_log else "", encoding="utf-8"
    )
    if unmatched_log:
        print(f"Logged {len(unmatched_log)} unmatched affiliation string(s) -> {UNMATCHED_LOG_PATH}")
        print("Add real entries to institutions.json for any of these you want to appear as pins.")


if __name__ == "__main__":
    main()
