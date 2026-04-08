"""
job_scraper.py  —  Enterprise job search via real aggregator APIs

Sources (in order of coverage):
  1. JSearch  (RapidAPI)  — aggregates Indeed, LinkedIn, Glassdoor, ZipRecruiter + 100s more
  2. Adzuna               — broad US market, excellent small-company coverage
  3. The Muse             — free, no key, tech & startup companies
  4. Remotive             — free, no key, remote-only positions
  5. USAJobs              — free, government / federal positions
  6. Arbeitnow            — free, no key, remote + international with US filter

All sources are filtered to US + Remote jobs only.
Results are deduplicated by title+company.

Required env vars (set in .env):
  ANTHROPIC_API_KEY   — already set
  RAPIDAPI_KEY        — sign up free at rapidapi.com → subscribe to "JSearch"
  ADZUNA_APP_ID       — sign up free at developer.adzuna.com
  ADZUNA_APP_KEY      — same as above
  USAJOBS_API_KEY     — sign up free at developer.usajobs.gov
  USAJOBS_EMAIL       — the email you registered with at usajobs.gov

Fixes applied:
  [FIX 1] JSearch & Adzuna now send quoted exact-phrase queries to the API
          e.g. '"Data Engineer" United States' instead of 'Data Engineer jobs in United States'
          This reduces garbage results at the source before filtering even runs.

  [FIX 2] _title_matches() now has 4 layers:
          Layer A — strip filler words, build query_words (unchanged)
          Layer B — ALL query words must appear as WHOLE words in title (unchanged)
          Layer C — query words must appear within 4 words of each other (proximity)
          Layer D — NEW: query words must appear in the SAME ORDER as the search query
                    e.g. searching "Data Engineer" → "data" must come BEFORE "engineer"
                    blocks "AI/ML Engineer (Data Focus)" where engineer precedes data
                    keeps "Data Engineer", "Data Platform Engineer", "Senior Data Engineer"
                    ORDER_FLEXIBLE = True  ← set to True to allow any order (less strict)

  [FIX 3] Null/empty title guard tightened — empty or whitespace-only titles
          are rejected before _title_matches() is even called.
"""

import os
import re
import time
import random
import urllib.parse
import requests
from datetime import datetime, timezone
from itertools import product as itertools_product


# ── Shared HTTP helper ────────────────────────────────────────────────────────

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": "JobPilot/2.0 (Enterprise Job Aggregator)",
    "Accept":     "application/json",
})


def _get(url: str, headers: dict = None, params: dict = None, timeout: int = 15) -> requests.Response | None:
    try:
        time.sleep(random.uniform(0.2, 0.6))
        h = dict(_SESSION.headers)
        if headers:
            h.update(headers)
        r = _SESSION.get(url, headers=h, params=params, timeout=timeout)
        r.raise_for_status()
        return r
    except Exception as e:
        print(f"  [scraper] GET failed: {url[:70]}  —  {e}")
        return None


# ── Date normalizer ───────────────────────────────────────────────────────────

def _normalize_date(raw) -> str:
    if not raw:
        return "Today"
    s = str(raw).strip()
    s_low = s.lower()
    if any(k in s_low for k in ("just", "moment", "now", "second")):
        return "Just now"
    if "hour" in s_low:
        n = re.search(r"(\d+)", s)
        return f"{n.group()} hr ago" if n else "Today"
    if "today" in s_low or s_low in ("0", "0 days"):
        return "Today"
    if "day" in s_low:
        n = re.search(r"(\d+)", s)
        return f"{n.group()} day{'s' if n and n.group() != '1' else ''} ago" if n else "Today"
    # ISO datetime
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
        d = delta.days
        hrs = delta.seconds // 3600
        if d == 0:
            return f"{hrs} hr ago" if hrs > 0 else "Just now"
        if d == 1:
            return "1 day ago"
        if d < 7:
            return f"{d} days ago"
        return s[:10]
    except Exception:
        return s[:10] if len(s) >= 10 else (s or "Today")


# ── Salary builder ────────────────────────────────────────────────────────────

def _salary(min_s, max_s, period=None) -> str:
    suffix = {"year": "/yr", "month": "/mo", "week": "/wk", "hour": "/hr"}.get(
        str(period or "").lower(), ""
    )
    try:
        lo = int(float(min_s)) if min_s else None
        hi = int(float(max_s)) if max_s else None
    except (ValueError, TypeError):
        return "Not listed"
    if lo and hi:
        return f"${lo:,} – ${hi:,}{suffix}"
    if lo:
        return f"${lo:,}+{suffix}"
    return "Not listed"


# ── Location builder ──────────────────────────────────────────────────────────

def _location(city=None, state=None, country=None, is_remote=False) -> str:
    if is_remote:
        return "Remote"
    parts = [p for p in (city, state) if p]
    if parts:
        return ", ".join(parts)
    return country or "United States"


# ── US / Remote filter ────────────────────────────────────────────────────────

_US_STATES = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN",
    "IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV",
    "NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN",
    "TX","UT","VT","VA","WA","WV","WI","WY","DC"
}
_REMOTE_KEYWORDS = {"remote", "united states", "usa", "u.s.", "us,", "worldwide", "anywhere", "work from home"}
_NON_US_COUNTRIES = {
    "united kingdom", "india", "canada", "australia", "germany", "france",
    "netherlands", "singapore", "brazil", "mexico", "spain", "italy",
    "poland", "uk", "gb", "eu", "europe"
}


def _is_us_or_remote(loc: str) -> bool:
    if not loc:
        return True
    u = loc.lower()
    if any(k in u for k in _REMOTE_KEYWORDS):
        return True
    # Reject known non-US countries
    if any(k in u for k in _NON_US_COUNTRIES):
        return False
    u_up = loc.upper()
    for state in _US_STATES:
        if f", {state}" in u_up or f" {state}," in u_up or u_up.endswith(f" {state}") or u_up.endswith(f",{state}"):
            return True
    # Short ambiguous string → keep
    if len(loc.split()) <= 3:
        return True
    return False


# ── Title relevance filter ────────────────────────────────────────────────────

def _title_matches(job_title: str, search_query: str) -> bool:
    """
    Return True only if the job title is genuinely relevant to the search query.

    4-layer check:

    Layer A — Build meaningful query words
        Strip filler/noise words (senior, remote, jr, etc.)
        Split remaining words from the search query
        e.g. "Senior Data Engineer" → ["data", "engineer"]

    Layer B — All query words must appear as WHOLE WORDS in the title
        Uses \\b word boundaries so:
          "data"     does NOT match inside "database"
          "engineer" does NOT match inside "engineering"

    Layer C — Proximity check
        Query words must appear within PROXIMITY_WINDOW words of each other.
        Blocks titles where words are spread far apart.
        Keeps: "Data Engineer", "Senior Data Engineer", "Data Platform Engineer"
        Blocks: "Director of AI — Data Solutions Engineer Support" (span > 4)

    Layer D — Word order check
        Query words must appear in the SAME ORDER as your search query.
        Searching "Data Engineer" → "data" must come BEFORE "engineer" in the title.

        Why this matters:
          "AI/ML Engineer (Data Focus)"
           title_words = [ai, ml, engineer, data, focus]
           "engineer" is at index 2, "data" is at index 3
           → engineer comes BEFORE data  →  wrong order  →  BLOCKED

          "Data Platform Engineer"
           "data" is at index 0, "engineer" is at index 2
           → data comes BEFORE engineer  →  correct order  →  PASSES

        ORDER_FLEXIBLE = False  ← set to True to allow any order (less strict)
            False = strict, recommended for most job title searches
            True  = flexible, use if you search things like "Engineer Data" style queries

    Tuning constants:
        PROXIMITY_WINDOW = 4   (3=very strict, 4=recommended, 6=loose)
        ORDER_FLEXIBLE   = False
    """

    FILLER_WORDS = {
        "senior", "sr", "jr", "junior", "lead", "staff", "principal",
        "associate", "head", "i", "ii", "iii", "iv",
        "the", "and", "or", "of", "in", "at", "a",
        "remote", "us", "usa", "jobs", "job"
    }

    PROXIMITY_WINDOW = 4    # max word-distance allowed between query words in title
    ORDER_FLEXIBLE   = False # if True, skip Layer D (word order check)

    # ── Layer A: build query words (preserving search order) ─────────────────
    query_words = [
        w.strip().lower() for w in re.split(r"[\s,/\-]+", search_query)
        if w.strip()
        and w.strip().lower() not in FILLER_WORDS
        and len(w.strip()) > 1
    ]

    # Nothing meaningful to filter on → keep all
    if not query_words:
        return True

    title_lower = job_title.lower()

    # ── Layer B: all query words must be present as whole words ───────────────
    for qw in query_words:
        if not re.search(r'\b' + re.escape(qw) + r'\b', title_lower):
            return False  # missing word → reject immediately

    # ── Layers C & D only apply when there are 2+ query words ─────────────────
    if len(query_words) >= 2:

        # Tokenize the title into an ordered list of words
        title_words = re.findall(r'\b\w+\b', title_lower)

        # Find every position each query word appears at in the title
        positions: dict[str, list[int]] = {}
        for qw in query_words:
            for i, tw in enumerate(title_words):
                if tw == qw:
                    positions.setdefault(qw, []).append(i)

        # Safety net (should never trigger after Layer B)
        if len(positions) < len(query_words):
            return False

        pos_lists = [positions[qw] for qw in query_words]

        # ── Layer C: proximity check ──────────────────────────────────────────
        # Find the minimum span (max_pos - min_pos) across all position combos.
        # If even the closest combo is too spread out, reject.
        min_span = min(
            max(combo) - min(combo)
            for combo in itertools_product(*pos_lists)
        )
        if min_span > PROXIMITY_WINDOW:
            return False  # words too far apart → likely not the right role

        # ── Layer D: word order check ─────────────────────────────────────────
        # Check that there exists at least ONE combination of positions where
        # each query word appears in strictly increasing order in the title.
        # e.g. query = ["data", "engineer"]
        #      we need pos(data) < pos(engineer) in the title
        #
        # "Data Platform Engineer" → pos(data)=0, pos(engineer)=2  → 0 < 2 ✓
        # "AI/ML Engineer (Data Focus)" → pos(engineer)=2, pos(data)=3
        #      only combo is (2, 3) → engineer(2) before data(3) → WRONG ORDER
        if not ORDER_FLEXIBLE:
            order_ok = any(
                all(combo[i] < combo[i + 1] for i in range(len(combo) - 1))
                for combo in itertools_product(*pos_lists)
            )
            if not order_ok:
                return False  # no combo preserves query word order → reject

    return True


# ── 1. JSearch (RapidAPI) ─────────────────────────────────────────────────────

def search_jsearch(title: str, location: str, pages: int = 5) -> list[dict]:
    """
    JSearch aggregates Indeed, LinkedIn, Glassdoor, ZipRecruiter, Monster,
    CareerBuilder and 200+ other platforms. Best single-source for all company sizes.
    Sign up free at: https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch
    Free tier: 200 requests/month.

    FIX: Query now uses quoted exact phrase e.g. '"Data Engineer" United States'
         so JSearch's own engine does tighter matching before we even filter.
    """
    key = os.environ.get("RAPIDAPI_KEY", "")
    if not key:
        print("  [jsearch] RAPIDAPI_KEY not set — skipping (sign up free at rapidapi.com)")
        return []

    jobs = []
    hdrs = {
        "X-RapidAPI-Key":  key,
        "X-RapidAPI-Host": "jsearch.p.rapidapi.com",
    }

    # FIX 1: Wrap title in quotes for exact-phrase matching at API level
    exact_query = f'"{title}" {location}'

    for page in range(1, pages + 1):
        r = _get(
            "https://jsearch.p.rapidapi.com/search",
            headers=hdrs,
            params={
                "query":       exact_query,
                "page":        str(page),
                "num_pages":   "1",
                "date_posted": "today",
                "country":     "us",
                "language":    "en",
            },
        )
        if not r:
            break
        try:
            data = r.json()
            batch = data.get("data", [])
            if not batch:
                break
            for j in batch:
                emp_type = (j.get("job_employment_type") or "").replace("_", " ").title()
                jobs.append({
                    "id":          f"jsearch_{j.get('job_id', len(jobs))}",
                    "title":       j.get("job_title", "").strip(),
                    "company":     j.get("employer_name", "Unknown"),
                    "location":    _location(
                                       j.get("job_city"), j.get("job_state"),
                                       j.get("job_country"), j.get("job_is_remote")
                                   ),
                    "posted":      _normalize_date(j.get("job_posted_at_datetime_utc")),
                    "salary":      _salary(j.get("job_min_salary"), j.get("job_max_salary"),
                                           j.get("job_salary_period")),
                    "url":         j.get("job_apply_link") or j.get("job_url", ""),
                    "source":      j.get("job_publisher", "JSearch"),
                    "description": (j.get("job_description") or "")[:4000],
                    "type":        emp_type,
                })
        except Exception as e:
            print(f"  [jsearch] Parse error page {page}: {e}")
            break

    print(f"  [jsearch] {len(jobs)} raw jobs fetched for '{title}'")
    return jobs


# ── 2. Adzuna ─────────────────────────────────────────────────────────────────

def search_adzuna(title: str, location: str, pages: int = 4) -> list[dict]:
    """
    Adzuna has excellent US coverage including solo founders, SMBs, and enterprises.
    Sign up free at: https://developer.adzuna.com/
    Free tier: 250 calls/day.

    FIX: 'what_phrase' param used for exact phrase match in Adzuna API.
    """
    app_id  = os.environ.get("ADZUNA_APP_ID", "")
    app_key = os.environ.get("ADZUNA_APP_KEY", "")
    if not app_id or not app_key:
        print("  [adzuna] ADZUNA_APP_ID/KEY not set — skipping (sign up free at developer.adzuna.com)")
        return []

    jobs = []
    for page in range(1, pages + 1):
        r = _get(
            f"https://api.adzuna.com/v1/api/jobs/us/search/{page}",
            params={
                "app_id":           app_id,
                "app_key":          app_key,
                "results_per_page": 20,
                "what_phrase":      title,
                "where":            location,
                "max_days_old":     1,
                "sort_by":          "date",
                "content-type":     "application/json",
            },
        )
        if not r:
            break
        try:
            data = r.json()
            batch = data.get("results", [])
            if not batch:
                break
            for j in batch:
                company  = j.get("company", {}).get("display_name", "Unknown")
                loc_area = j.get("location", {}).get("area", [])
                loc_str  = ", ".join(loc_area[-2:]) if loc_area else location
                jobs.append({
                    "id":          f"adzuna_{j.get('id', len(jobs))}",
                    "title":       j.get("title", "").strip(),
                    "company":     company,
                    "location":    loc_str,
                    "posted":      _normalize_date(j.get("created")),
                    "salary":      _salary(j.get("salary_min"), j.get("salary_max"), "year"),
                    "url":         j.get("redirect_url", ""),
                    "source":      "Adzuna",
                    "description": j.get("description", "")[:4000],
                    "type":        j.get("contract_time", "").replace("_", " ").title(),
                })
        except Exception as e:
            print(f"  [adzuna] Parse error page {page}: {e}")
            break

    print(f"  [adzuna] {len(jobs)} raw jobs fetched for '{title}'")
    return jobs


# ── 3. The Muse (free, no key) ────────────────────────────────────────────────

def search_themuse(title: str, location: str, pages: int = 4) -> list[dict]:
    """
    The Muse covers 100+ companies with transparent culture info.
    Completely free — no API key needed.
    """
    jobs = []
    loc_map = {
        "seattle":       "Seattle, WA",
        "new york":      "New York City, NY",
        "san francisco": "San Francisco, CA",
        "los angeles":   "Los Angeles, CA",
        "austin":        "Austin, TX",
        "chicago":       "Chicago, IL",
        "boston":        "Boston, MA",
        "denver":        "Denver, CO",
        "atlanta":       "Atlanta, GA",
        "dallas":        "Dallas, TX",
        "houston":       "Houston, TX",
        "miami":         "Miami, FL",
        "washington":    "Washington, DC",
        "remote":        "Flexible / Remote",
    }
    loc_lower = location.lower()
    muse_loc  = next((v for k, v in loc_map.items() if k in loc_lower), None)

    params: dict = {"descending": "true"}
    if muse_loc:
        params["location"] = muse_loc
    params["category"] = title

    for page in range(1, pages + 1):
        params["page"] = page
        r = _get("https://www.themuse.com/api/public/jobs", params=params)
        if not r:
            break
        try:
            data  = r.json()
            batch = data.get("results", [])
            if not batch:
                break
            for j in batch:
                locs    = j.get("locations", [])
                loc_str = locs[0].get("name", location) if locs else location
                company = j.get("company", {}).get("name", "Unknown")
                jobs.append({
                    "id":          f"muse_{j.get('id', len(jobs))}",
                    "title":       j.get("name", "").strip(),
                    "company":     company,
                    "location":    loc_str,
                    "posted":      _normalize_date(j.get("publication_date")),
                    "salary":      "See listing",
                    "url":         j.get("refs", {}).get("landing_page", ""),
                    "source":      "The Muse",
                    "description": "",
                    "type":        j.get("type", "").replace("_", " ").title(),
                })
        except Exception as e:
            print(f"  [themuse] Parse error: {e}")
            break

    print(f"  [themuse] {len(jobs)} raw jobs fetched for '{title}'")
    return jobs


# ── 4. Remotive (free, remote jobs) ──────────────────────────────────────────

def search_remotive(title: str) -> list[dict]:
    """
    Remotive is the leading remote-jobs platform. Free API, no key required.
    Covers companies from Shopify to solo startups, all remote positions.

    NOTE: Remotive matches on full description text, not just title.
    Our _title_matches() filter in search_all_platforms() handles this —
    any job whose *title* doesn't match your search is discarded afterward.
    """
    jobs = []
    r = _get(
        "https://remotive.com/api/remote-jobs",
        params={"search": title, "limit": 100},
    )
    if not r:
        return jobs
    try:
        for j in r.json().get("jobs", []):
            region = j.get("candidate_required_location") or "Worldwide"
            jobs.append({
                "id":          f"remotive_{j.get('id', len(jobs))}",
                "title":       j.get("title", "").strip(),
                "company":     j.get("company_name", "Unknown"),
                "location":    f"Remote — {region}",
                "posted":      _normalize_date(j.get("publication_date")),
                "salary":      j.get("salary") or "See listing",
                "url":         j.get("url", ""),
                "source":      "Remotive",
                "description": (j.get("description") or "")[:4000],
                "type":        "Remote",
            })
    except Exception as e:
        print(f"  [remotive] Parse error: {e}")

    print(f"  [remotive] {len(jobs)} raw jobs fetched for '{title}'")
    return jobs


# ── 5. USAJobs (free, government) ────────────────────────────────────────────

def search_usajobs(title: str, location: str) -> list[dict]:
    """
    Official US federal government job portal.
    Sign up free at: https://developer.usajobs.gov/
    Or works without a key with reduced rate limits.
    """
    email   = os.environ.get("USAJOBS_EMAIL", "jobpilot@example.com")
    api_key = os.environ.get("USAJOBS_API_KEY", "")

    hdrs = {"Host": "data.usajobs.gov", "User-Agent": email}
    if api_key:
        hdrs["Authorization-Key"] = api_key

    r = _get(
        "https://data.usajobs.gov/api/search",
        headers=hdrs,
        params={
            "Keyword":        title,
            "LocationName":   location,
            "DatePosted":     1,
            "ResultsPerPage": 25,
            "SortField":      "OpenDate",
            "SortDirection":  "Desc",
        },
    )
    jobs = []
    if not r:
        return jobs
    try:
        items = r.json().get("SearchResult", {}).get("SearchResultItems", [])
        for item in items:
            d   = item.get("MatchedObjectDescriptor", {})
            sal = (d.get("PositionRemuneration") or [{}])[0]
            sal_str = _salary(sal.get("MinimumRange"), sal.get("MaximumRange"), "year")
            url = ""
            uris = d.get("ApplyURI") or d.get("PositionURI") or []
            if isinstance(uris, list) and uris:
                url = uris[0]
            elif isinstance(uris, str):
                url = uris
            jobs.append({
                "id":          f"usajobs_{d.get('PositionID', len(jobs))}",
                "title":       d.get("PositionTitle", "").strip(),
                "company":     d.get("OrganizationName", "US Federal Government"),
                "location":    d.get("PositionLocationDisplay", location),
                "posted":      _normalize_date(d.get("PublicationStartDate")),
                "salary":      sal_str,
                "url":         url,
                "source":      "USAJobs",
                "description": d.get("UserArea", {}).get("Details", {}).get("JobSummary", "")[:4000],
                "type":        (d.get("PositionSchedule") or [{}])[0].get("Name", ""),
            })
    except Exception as e:
        print(f"  [usajobs] Parse error: {e}")

    print(f"  [usajobs] {len(jobs)} raw jobs fetched for '{title}'")
    return jobs


# ── 6. Arbeitnow (free, global with US/remote filter) ────────────────────────

def search_arbeitnow(title: str) -> list[dict]:
    """
    Arbeitnow is a free job board aggregator with remote and US positions.
    No API key required. Filter applied: remote=true or US location only.

    NOTE: Like Remotive, Arbeitnow searches full descriptions.
    _title_matches() in search_all_platforms() cleans up the noise afterward.
    """
    jobs = []
    for page in range(1, 4):
        r = _get(
            "https://www.arbeitnow.com/api/job-board-api",
            params={"search": title, "page": page},
        )
        if not r:
            break
        try:
            data  = r.json()
            batch = data.get("data", [])
            if not batch:
                break
            for j in batch:
                loc = j.get("location") or ""
                is_remote = bool(j.get("remote"))
                if not is_remote and not _is_us_or_remote(loc):
                    continue
                jobs.append({
                    "id":          f"arbeitnow_{j.get('slug', len(jobs))}",
                    "title":       j.get("title", "").strip(),
                    "company":     j.get("company_name", "Unknown"),
                    "location":    "Remote" if is_remote else loc,
                    "posted":      _normalize_date(str(j.get("created_at", ""))),
                    "salary":      "See listing",
                    "url":         j.get("url", ""),
                    "source":      "Arbeitnow",
                    "description": (j.get("description") or "")[:4000],
                    "type":        "Remote" if is_remote else "",
                })
        except Exception as e:
            print(f"  [arbeitnow] Parse error page {page}: {e}")
            break

    print(f"  [arbeitnow] {len(jobs)} raw jobs fetched for '{title}'")
    return jobs


# ── Fetch full job description from URL ───────────────────────────────────────

def fetch_job_description(url: str) -> str:
    """Fetch and extract job description text from any job posting URL."""
    from bs4 import BeautifulSoup
    try:
        time.sleep(random.uniform(0.4, 1.0))
        r = requests.get(url, headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }, timeout=14)
        r.raise_for_status()
    except Exception as e:
        print(f"  [jd] Fetch failed: {e}")
        return ""

    soup = BeautifulSoup(r.text, "html.parser")
    for sel in [
        "#jobDescriptionText",
        "div[data-testid='jobDescriptionText']",
        ".jobsearch-jobDescriptionText",
        "div.job-description",
        "div.jobDescription",
        "section.description",
        "div[class*='description']",
        "div[class*='job-detail']",
        "div[class*='jobDetail']",
        ".job-desc",
        "article.job__description",
        "[data-automation='jobDescription']",
        ".job-view-layout",
        "div[class*='job_description']",
    ]:
        el = soup.select_one(sel)
        if el and len(el.get_text(strip=True)) > 100:
            return el.get_text(separator="\n", strip=True)[:5000]

    divs = [d for d in soup.find_all("div") if len(d.get_text(strip=True)) > 300]
    if divs:
        return max(divs, key=lambda d: len(d.get_text())).get_text(separator="\n", strip=True)[:5000]
    return ""


# ── Master search ─────────────────────────────────────────────────────────────

def search_all_platforms(title: str, location: str = "United States") -> list[dict]:
    """
    Search all real job APIs and return deduplicated US + remote results.
    Covers FAANG, Fortune 500, mid-market, startups, and solo companies.

    Filtering pipeline (in order):
      1. US/Remote location filter      — drop non-US jobs
      2. Non-empty title guard          — drop blank titles
      3. _title_matches() filter        — drop irrelevant titles (4-layer check)
      4. Deduplication                  — drop exact (title, company) duplicates
    """
    print(f"\n[scraper] Searching: '{title}' in '{location}'")
    all_jobs: list[dict] = []

    scrapers = [
        lambda: search_jsearch(title, location, pages=5),
        lambda: search_adzuna(title, location, pages=4),
        lambda: search_themuse(title, location, pages=4),
        lambda: search_remotive(title),
        lambda: search_usajobs(title, location),
        lambda: search_arbeitnow(title),
    ]

    for fn in scrapers:
        try:
            all_jobs.extend(fn())
        except Exception as e:
            print(f"  [scraper] Source error: {e}")

    total_raw = len(all_jobs)

    # ── Filter 1: US or Remote only ───────────────────────────────────────────
    all_jobs = [j for j in all_jobs if _is_us_or_remote(j.get("location", ""))]
    print(f"[filter] After location filter : {len(all_jobs):>4} / {total_raw}")

    # ── Filter 2: Must have a non-empty title ─────────────────────────────────
    all_jobs = [j for j in all_jobs if j.get("title", "").strip()]
    print(f"[filter] After empty-title drop: {len(all_jobs):>4}")

    # ── Filter 3: Title must be relevant to the search query ──────────────────
    #    _title_matches() runs 4 layers:
    #      A) query word extraction (strips fillers)
    #      B) whole-word presence check (word boundary regex)
    #      C) proximity check (words must be within 4 positions of each other)
    #      D) word order check (words must appear in same order as search query)
    before_title_filter = len(all_jobs)
    all_jobs = [j for j in all_jobs if _title_matches(j.get("title", ""), title)]
    print(f"[filter] After title filter    : {len(all_jobs):>4}  "
          f"(removed {before_title_filter - len(all_jobs)} irrelevant titles)")

    # ── Filter 4: Deduplicate by (title_lower, company_lower) ─────────────────
    seen:   set  = set()
    unique: list = []
    for j in all_jobs:
        key = (j["title"].lower().strip()[:60], j["company"].lower().strip()[:50])
        if key not in seen:
            seen.add(key)
            unique.append(j)

    print(f"[filter] After deduplication   : {len(unique):>4}  "
          f"(removed {len(all_jobs) - len(unique)} duplicates)")

    # Re-index
    for i, j in enumerate(unique):
        j["idx"] = i

    print(f"\n[scraper] Final results: {len(unique)} unique jobs from {_source_count(unique)} sources")
    return unique


def _source_count(jobs: list[dict]) -> int:
    return len({j.get("source", "") for j in jobs})
