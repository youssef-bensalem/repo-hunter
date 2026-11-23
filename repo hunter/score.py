"""Step 2: drop bad candidates and rank the rest by how fast they are gaining stars."""
import datetime as dt
import re

import config


def _parse(ts):
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))


def reject_reason(repo, seen, now):
    """Return why a repo is rejected, or None if it passes."""
    if repo["full_name"].lower() in seen:
        return "already sent"
    if repo["archived"]:
        return "archived"
    if repo["fork"]:
        return "fork"
    if not repo["license"]:
        return "no license"
    if not repo["description"]:
        return "no description"
    if (now - _parse(repo["pushed_at"])).days > config.MAX_INACTIVE_DAYS:
        return "inactive"
    name_text = repo["full_name"].split("/", 1)[1] + " " + " ".join(repo["topics"])
    if re.search(config.EXCLUDE_NAME_PATTERN, name_text, re.I):
        return "excluded name/topic"
    if re.search(config.EXCLUDE_TEXT_PATTERN, f"{name_text} {repo['description']}", re.I):
        return "spam/malware keywords"
    if config.ENGLISH_ONLY:
        letters = [c for c in repo["description"] if c.isalpha()]
        if letters and sum(ord(c) > 0x024F for c in letters) / len(letters) > 0.3:
            return "non-English description"
    return None


def score(repo, now):
    """Stars per day of age. Repos people are discussing on HN/Reddit right now get a boost,
    and their age is capped at 30 days so an older repo that's suddenly popular can still win."""
    age_days = max((now - _parse(repo["created_at"])).total_seconds() / 86400, 1.0)
    community = [s for s in repo["sources"] if s != "github"]
    if community:
        age_days = min(age_days, 30.0)
    return round(repo["stars"] / age_days * (1 + 0.5 * len(community)), 2)


def rank(repos, seen, now=None, verbose=False):
    now = now or dt.datetime.now(dt.timezone.utc)
    kept = []
    for repo in repos:
        reason = reject_reason(repo, seen, now)
        if reason:
            if verbose:
                print(f"  skip {repo['full_name']}: {reason}")
            continue
        s = score(repo, now)
        flag = ("gaining stars unusually fast: check for bought stars / malware before posting"
                if s >= config.SUSPICIOUS_STARS_PER_DAY else None)
        kept.append({**repo, "score": s, "flag": flag})
    return sorted(kept, key=lambda r: r["score"], reverse=True)
