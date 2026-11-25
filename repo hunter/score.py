"""Step 2: drop bad candidates and rank the rest by how fast they are gaining stars."""
import datetime as dt
import re

import config


def _parse(ts):
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))


def content_reason(repo):
    """Checks on what a repo *is* (name, topics, description): usable on old history entries too."""
    repo = {"topics": [], **repo}
    name_text = repo["full_name"].split("/", 1)[1] + " " + " ".join(repo["topics"])
    if re.search(config.EXCLUDE_NAME_PATTERN, name_text, re.I):
        return "excluded name/topic"
    if re.search(config.EXCLUDE_TEXT_PATTERN, f"{name_text} {repo['description']}", re.I):
        return "spam/malware keywords"
    if re.search(r"\bupstream\s*:|\bmirror of\b|\b(re-?upload|fork|copy) of\b|\btranslated (from|version of)\b|"
                 r"\b(english|chinese) translation of\b|\(english translation", repo["description"], re.I):
        return "re-upload or mirror of another repo"
    if config.SKIP_GAME_PORTS and re.search(config.GAME_PORT_PATTERN, f"{name_text} {repo['description']}", re.I):
        return "game port/emulator (copyright & malware risk)"
    if config.ENGLISH_ONLY:
        letters = [c for c in repo["description"] if c.isalpha()]
        if letters and sum(ord(c) > 0x024F for c in letters) / len(letters) > 0.3:
            return "non-English description"
    return None


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
    reason = content_reason(repo)
    if reason:
        return reason
    if len(repo["description"].split()) < 3:
        return "description too short"
    return None


# ---------- categories & "alternative to X" ----------

CATEGORIES = [   # label, keywords. The label with the most hits wins (description counts double).
    ("AI", r"\b(ai|llms?|agents?|agentic|gpt|claude|gemini|openai|deepseek|qwen|ml|rag|mcp|diffusion|"
           r"inference|transformers?|embeddings?|chatbot|copilot|codex|prompts?|neural)\b"),
    ("Security", r"\b(security|privacy|spyware|malware|encryption|pentest\w*|vulnerab\w*|firewall|passwords?)\b"),
    ("Self-hosted", r"\b(self ?hosted|homelab|home ?server|docker ?compose|nas)\b"),
    ("Design & media", r"\b(design|figma|canva|video|videos|image|images|photos?|audio|music|pixel|3d|cad|"
                       r"animations?|drawing|sound)\b"),
    ("Dev tool", r"\b(cli|terminal|shell|ide|code editor|devtools?|developer tools|compiler|builds?|git|"
                 r"debugger|framework|sdk|database|containers?|runtime|testing|codebase|programming)\b"),
    ("Productivity", r"\b(notes?|notion|markdown|calendar|todo|tasks?|productivity|meetings?|office|pdf|email|"
                     r"word processor|spreadsheet|documents?|download manager)\b"),
    ("Desktop app", r"\b(macos|mac|windows|linux|desktop|android|ios|iphone|tv)\b"),
]

_NOT_A_NAME = {"a", "an", "the", "free", "open", "best", "great", "simple", "modern", "fast", "lightweight",
               "native", "local", "private", "better", "drop-in", "tiny", "minimal", "self-hosted", "full"}
_PLATFORMS = {"windows", "mac", "macos", "linux", "android", "ios", "iphone", "ipad", "web", "everyone", "developers",
              "teams", "the", "your", "you"}
_ALT_TO = re.compile(r"(?:alternative|replacement)\s+(?:to|for)\s+([A-Z][\w.+-]*(?:(?:,\s*|\s+(?:and|or)\s+)"
                     r"[A-Z][\w.+-]*)*)")
_X_ALT = re.compile(r"([A-Za-z][\w.+-]*)\s+alternative\b")
_CLONE_OF = re.compile(r"(?:open[- ]source|free)\s+(?:version|clone)\s+of\s+([A-Z][\w.+-]*)")


def categorize(repo):
    desc = f"{repo['full_name'].split('/', 1)[1]} {repo['description']}".replace("-", " ").replace("_", " ")
    topics = " ".join(repo.get("topics") or []).replace("-", " ").replace("_", " ")
    best, best_score = "Other", 0
    for label, pattern in CATEGORIES:
        hits = 2 * len(re.findall(pattern, desc, re.I)) + len(re.findall(pattern, topics, re.I))
        if label == "Desktop app" and best_score:
            break                     # platform names only decide when nothing else matched
        if hits > best_score:
            best, best_score = label, hits
    return best


def alternative_to(repo):
    """'Notion' for "an open-source alternative to Notion"; None if the repo doesn't say it replaces something."""
    text = repo["description"]
    for rx in (_ALT_TO, _CLONE_OF):
        for m in rx.finditer(text):
            name = m.group(1).rstrip(".,")
            if name.split(",")[0].split()[0].lower() not in _PLATFORMS:
                return name
    for m in _X_ALT.finditer(text):           # "Open source canva alternative"
        if m.group(1).lower() not in _NOT_A_NAME | _PLATFORMS:
            name = m.group(1)
            return name if name[0].isupper() else name.capitalize()
    for t in repo.get("topics") or []:        # topics like "granola-alternative"
        tm = re.fullmatch(r"([a-z0-9.+]+)-alternative", t)
        if tm:
            return tm.group(1).upper() if len(tm.group(1)) <= 3 else tm.group(1).capitalize()
    return None


def score(repo, now):
    """Stars per day of age. Repos people are discussing on HN/Reddit right now get a boost,
    and their age is capped at 30 days so an older repo that's suddenly popular can still win."""
    age_days = max((now - _parse(repo["created_at"])).total_seconds() / 86400, 1.0)
    community = [s for s in repo["sources"] if s != "github"]
    if community:
        age_days = min(age_days, 30.0)
    return round(repo["stars"] / age_days * (1 + 0.5 * len(community)), 2)


def recent_owners(history, now=None, days=None):
    """Owners featured in the last few days (lower case)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    days = config.OWNER_COOLDOWN_DAYS if days is None else days
    since = (now - dt.timedelta(days=days)).date().isoformat()
    return {h["full_name"].split("/")[0].lower() for h in history if h.get("date", "") >= since}


def variety_block(repo, picks, recent, max_per_category=None):
    """Why this repo would hurt today's variety (or None): same owner as a pick, an owner featured
    recently, or a category that already has its share. Counts only repos actually picked."""
    max_per_category = config.MAX_PER_CATEGORY if max_per_category is None else max_per_category
    owner = repo["owner"].lower()
    if owner in recent:
        return "owner featured in the last few days"
    if any(p["owner"].lower() == owner for p in picks):
        return "same owner as another pick today"
    cat = repo.get("category", "Other")
    if sum(p.get("category", "Other") == cat for p in picks) >= max_per_category:
        return f"already {max_per_category} {cat} picks today"
    return None


def suspicion(repo, stars_per_day):
    """Why a repo deserves a closer look before posting (bought stars/forks are common), or None."""
    if stars_per_day >= config.SUSPICIOUS_STARS_PER_DAY:
        return "gaining stars unusually fast: check for bought stars / malware before posting"
    if repo["stars"] >= 1000 and repo.get("forks", 0) / repo["stars"] > 0.5:
        return (f"{repo['forks']:,} forks for {repo['stars']:,} stars is unusual (often bot accounts): "
                "check it before posting")
    return None


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
        flag = suspicion(repo, s)
        replaces = alternative_to(repo)
        if replaces:                      # "free alternative to X" posts get shared the most
            s = round(s * config.ALTERNATIVE_BOOST, 2)
        kept.append({**repo, "score": s, "flag": flag, "category": categorize(repo), "replaces": replaces})
    return sorted(kept, key=lambda r: r["score"], reverse=True)
