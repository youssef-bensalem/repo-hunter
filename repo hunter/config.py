"""All settings live here.

Every value can be overridden with an environment variable of the same name
(in GitHub Actions: Settings -> Secrets and variables -> Actions -> Variables).
Empty values fall back to the defaults below.
"""
import os


def _env(name, default):
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def _int(name, default):
    return int(_env(name, str(default)))


def _list(name, default):
    raw = _env(name, "")
    return [x.strip() for x in raw.split(",") if x.strip()] if raw else default


# What the account is about (used in the LLM prompt).
NICHE = _env("NICHE", "useful open-source tools and GitHub repos for developers, students and AI/ML builders")

# --- Collection ---
DAYS_BACK = _int("DAYS_BACK", 14)            # GitHub search: repos created in the last N days
MIN_STARS = _int("MIN_STARS", 100)           # ...with more than N stars
TOPICS = _list("TOPICS", [])                 # optional extra searches, e.g. "self-hosted,llm,cli"
SUBREDDITS = _list("SUBREDDITS", ["selfhosted", "opensource", "coolgithubprojects", "LocalLLaMA"])
HN_DAYS = _int("HN_DAYS", 3)                 # Show HN posts from the last N days
HN_MIN_POINTS = _int("HN_MIN_POINTS", 20)
MAX_COMMUNITY_LOOKUPS = _int("MAX_COMMUNITY_LOOKUPS", 40)  # caps GitHub API calls for HN/Reddit links

# --- Filtering ---
TOP_N = _int("TOP_N", 5)                     # drafts sent to Discord per day
MAX_INACTIVE_DAYS = _int("MAX_INACTIVE_DAYS", 30)
MIN_README_CHARS = _int("MIN_README_CHARS", 500)
# Checked against the repo name + topics.
EXCLUDE_NAME_PATTERN = _env("EXCLUDE_NAME_PATTERN", r"\bawesome\b|dotfiles|homework|assignment")
# Checked against name + topics + description. Catches the fake-star malware/spam repos.
EXCLUDE_TEXT_PATTERN = _env(
    "EXCLUDE_TEXT_PATTERN",
    r"\bcrack(ed|s)?\b|keygen|\bcheats?\b|aimbot|airdrop|robux|spoofer|injector|free[- ]download|activator",
)

ENGLISH_ONLY = _env("ENGLISH_ONLY", "true").lower() == "true"   # skip repos with non-Latin descriptions
# Faster than this (stars/day) gets a warning in Discord: often bought stars / malware bait.
SUSPICIOUS_STARS_PER_DAY = _int("SUSPICIOUS_STARS_PER_DAY", 3000)

# --- LLM (any OpenAI-compatible API; default = Gemini free tier) ---
LLM_API_KEY = _env("LLM_API_KEY", "")
LLM_BASE_URL = _env("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai").rstrip("/")
LLM_MODEL = _env("LLM_MODEL", "gemini-flash-latest")
LLM_DELAY = _int("LLM_DELAY", 5)             # seconds between LLM calls (stay under free-tier rate limits)

# --- Delivery ---
DISCORD_WEBHOOK_URL = _env("DISCORD_WEBHOOK_URL", "")
ACCOUNT_HANDLE = _env("ACCOUNT_HANDLE", "")      # your X handle, printed on the post images
CARD_ACCENT = _env("CARD_ACCENT", "#3FD07A")      # accent color of the post images
GITHUB_TOKEN = _env("GITHUB_TOKEN", _env("GH_TOKEN", ""))

POST_MAX_CHARS = 280
DATA_DIR = _env("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
