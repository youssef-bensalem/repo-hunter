"""Entry point.

    python main.py daily              # collect, score, draft, send to Discord
    python main.py weekly             # thread draft from this week's picks
    python main.py daily --dry-run    # print to the terminal, change nothing
"""
import argparse
import datetime as dt
import json
import os
import sys
import time

import config
from card import find_logo_urls
from collect import collect, fetch_readme, fetch_x_handle, gh_session
from draft import clean_readme, draft_post, draft_thread
from notify import build_daily_messages, build_weekly_messages, send
from score import rank


def _path(name):
    return os.path.join(config.DATA_DIR, name)


def load_json(name, default):
    try:
        with open(_path(name), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(name, data):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(_path(name), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=isinstance(data, dict))
        f.write("\n")


def run_daily(dry_run=False, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    today = now.date().isoformat()
    session = gh_session(config.GITHUB_TOKEN)
    seen = load_json("seen.json", {})
    history = load_json("history.json", [])

    candidates = collect(session, now=now)
    ranked = rank(candidates, seen, now=now, verbose=dry_run)
    print(f"{len(candidates)} candidates, {len(ranked)} passed filters")

    picks, failures = [], 0
    for repo in ranked:
        if len(picks) >= config.TOP_N:
            break
        readme = fetch_readme(session, repo["full_name"])
        if readme is None:
            failures += 1
            if failures >= 5:
                print("[warn] 5 README fetches failed in a row (rate limit or outage?), stopping early")
                break
            continue
        failures = 0
        if len(readme) < config.MIN_README_CHARS:
            print(f"  skip {repo['full_name']}: README too short")
            continue
        if picks and picks[-1]["used_llm"]:
            time.sleep(config.LLM_DELAY)
        handle = fetch_x_handle(session, repo["owner"])
        draft = draft_post(repo, clean_readme(readme), handle)
        picks.append({**repo, **draft, "x_handle": handle, "logo_urls": find_logo_urls(readme, repo)})
        print(f"  pick {repo['full_name']} (score {repo['score']}, {draft['chars']} chars)")

    send(config.DISCORD_WEBHOOK_URL, build_daily_messages(picks, today), dry_run=dry_run)

    if not dry_run:
        for p in picks:
            seen[p["full_name"].lower()] = today
            history.append({k: p.get(k) for k in
                            ("full_name", "url", "description", "stars", "language", "score", "text")} | {"date": today})
        cutoff = (now - dt.timedelta(days=60)).date().isoformat()
        save_json("seen.json", seen)
        save_json("history.json", [h for h in history if h["date"] >= cutoff])
    return picks


def run_weekly(dry_run=False, now=None, size=5):
    now = now or dt.datetime.now(dt.timezone.utc)
    week_start = (now - dt.timedelta(days=7)).date().isoformat()
    week = [h for h in load_json("history.json", []) if h["date"] >= week_start]
    top = sorted(week, key=lambda h: h["score"], reverse=True)[:size]
    tweets = draft_thread(top) if top else []
    send(config.DISCORD_WEBHOOK_URL, build_weekly_messages(tweets, now.date().isoformat()), dry_run=dry_run)
    return tweets


def main(argv=None):
    p = argparse.ArgumentParser(description="Find rising GitHub repos and send X drafts to Discord.")
    p.add_argument("mode", nargs="?", default="daily", choices=["daily", "weekly"])
    p.add_argument("--dry-run", action="store_true", help="print instead of sending; don't update data/")
    args = p.parse_args(argv)

    if not args.dry_run and not config.DISCORD_WEBHOOK_URL:
        sys.exit("DISCORD_WEBHOOK_URL is not set. Add it as a secret, or use --dry-run to print instead.")
    if args.mode == "daily":
        run_daily(dry_run=args.dry_run)
    else:
        run_weekly(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
