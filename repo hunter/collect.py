"""Step 1: collect candidate repos from GitHub search, Hacker News (Show HN) and Reddit."""
import datetime as dt
import re
import time

import requests

import config

GH_API = "https://api.github.com"
UA = "repo-hunter/1.0"

# github.com/<owner>/<repo>, but not gist.github.com etc.
_REPO_RE = re.compile(r"(?<![\w.-])(?:www\.)?github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9._-]{1,100})")
_NOT_OWNERS = {
    "about", "apps", "collections", "enterprise", "events", "explore", "features", "login",
    "marketplace", "orgs", "pricing", "settings", "sponsors", "topics", "trending", "users",
}


def gh_session(token=None):
    s = requests.Session()
    s.headers.update({
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": UA,
    })
    if token:
        s.headers["Authorization"] = f"Bearer {token}"
    return s


def normalize(item, source):
    return {
        "full_name": item["full_name"],
        "url": item["html_url"],
        "owner": item["owner"]["login"],
        "description": (item.get("description") or "").strip(),
        "stars": item.get("stargazers_count", 0),
        "forks": item.get("forks_count", 0),
        "language": item.get("language"),
        "topics": item.get("topics") or [],
        "license": (item.get("license") or {}).get("spdx_id"),
        "homepage": item.get("homepage") or "",
        "created_at": item["created_at"],
        "pushed_at": item["pushed_at"],
        "archived": bool(item.get("archived")),
        "fork": bool(item.get("fork")),
        "sources": [source],
    }


def extract_repo_names(text):
    """Return unique 'owner/repo' strings found in any text (URLs, post bodies)."""
    found, keys = [], set()
    for owner, repo in _REPO_RE.findall(text or ""):
        repo = repo.removesuffix(".git").rstrip(".")
        if not repo or owner.lower() in _NOT_OWNERS:
            continue
        name = f"{owner}/{repo}"
        if name.lower() not in keys:
            keys.add(name.lower())
            found.append(name)
    return found


# ---------- sources ----------

def search_github(session, days_back=None, min_stars=None, topics=None, now=None):
    days_back = config.DAYS_BACK if days_back is None else days_back
    min_stars = config.MIN_STARS if min_stars is None else min_stars
    topics = config.TOPICS if topics is None else topics
    now = now or dt.datetime.now(dt.timezone.utc)

    since = (now - dt.timedelta(days=days_back)).date().isoformat()
    queries = [f"created:>{since} stars:>{min_stars}"]
    queries += [f"created:>{since} stars:>{max(min_stars // 4, 20)} topic:{t}" for t in topics]

    results = []
    for i, q in enumerate(queries):
        if i:
            time.sleep(2)  # search API allows 30 requests/min with a token
        try:
            r = session.get(f"{GH_API}/search/repositories",
                            params={"q": q, "sort": "stars", "order": "desc", "per_page": 50}, timeout=30)
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"[warn] GitHub search failed for {q!r}: {e}")
            continue
        results += [normalize(item, "github") for item in r.json().get("items", [])]
    return results


def names_from_hacker_news(days=None, min_points=None):
    days = config.HN_DAYS if days is None else days
    min_points = config.HN_MIN_POINTS if min_points is None else min_points
    since = int(time.time()) - days * 86400
    try:
        r = requests.get("https://hn.algolia.com/api/v1/search_by_date",
                         params={"tags": "show_hn", "numericFilters": f"created_at_i>{since},points>{min_points}",
                                 "hitsPerPage": 200},
                         headers={"User-Agent": UA}, timeout=30)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"[warn] Hacker News fetch failed: {e}")
        return []
    out = []
    for hit in r.json().get("hits", []):
        for name in extract_repo_names(f"{hit.get('url') or ''} {hit.get('story_text') or ''}"):
            out.append((name, "hn"))
    return out


def names_from_reddit(subreddits=None):
    subreddits = config.SUBREDDITS if subreddits is None else subreddits
    out = []
    for sub in subreddits:
        try:
            r = requests.get(f"https://www.reddit.com/r/{sub}/top.json", params={"t": "day", "limit": 50},
                             headers={"User-Agent": f"{UA} (daily repo digest)"}, timeout=30)
            r.raise_for_status()
            posts = r.json()["data"]["children"]
        except (requests.RequestException, KeyError, ValueError) as e:
            print(f"[warn] Reddit r/{sub} fetch failed: {e}")
            continue
        for post in posts:
            d = post.get("data", {})
            for name in extract_repo_names(f"{d.get('url') or ''} {d.get('selftext') or ''}"):
                out.append((name, f"r/{sub}"))
        time.sleep(1)
    return out


# ---------- GitHub lookups ----------

def fetch_repo(session, full_name):
    try:
        r = session.get(f"{GH_API}/repos/{full_name}", timeout=30)
        if r.status_code in (404, 451):
            return None
        r.raise_for_status()
        return normalize(r.json(), "github")
    except requests.RequestException as e:
        print(f"[warn] repo lookup failed for {full_name}: {e}")
        return None


def fetch_readme(session, full_name):
    """README text, "" if the repo has none, or None if the request failed."""
    try:
        r = session.get(f"{GH_API}/repos/{full_name}/readme",
                        headers={"Accept": "application/vnd.github.raw+json"}, timeout=30)
        if r.status_code == 404:
            return ""
        r.raise_for_status()
        return r.text
    except requests.RequestException as e:
        print(f"[warn] README fetch failed for {full_name}: {e}")
        return None


def fetch_x_handle(session, login):
    """The owner's X username if they put one on their GitHub profile (users and orgs)."""
    try:
        r = session.get(f"{GH_API}/users/{login}", timeout=30)
        r.raise_for_status()
        handle = (r.json().get("twitter_username") or "").strip().lstrip("@")
        return handle or None
    except (requests.RequestException, ValueError):
        return None


# ---------- main entry ----------

def collect(session, now=None):
    pool = {}

    def add(repo):
        key = repo["full_name"].lower()
        if key not in pool:
            pool[key] = repo
            return
        for src in repo["sources"]:
            if src not in pool[key]["sources"]:
                pool[key]["sources"].append(src)

    for repo in search_github(session, now=now):
        add(repo)

    community = names_from_hacker_news() + names_from_reddit()
    lookups = 0
    for name, source in community:
        key = name.lower()
        if key in pool:
            if source not in pool[key]["sources"]:
                pool[key]["sources"].append(source)
            continue
        if lookups >= config.MAX_COMMUNITY_LOOKUPS:
            continue
        lookups += 1
        repo = fetch_repo(session, name)
        if repo:
            repo["sources"] = [source]
            add(repo)

    return list(pool.values())
