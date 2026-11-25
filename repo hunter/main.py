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
import tempfile
import time
import traceback

import requests

import config
from card import find_install, find_logo_urls
from collect import GH_API, collect, fetch_readme, fetch_x_handle, gh_session, search_github
import draft
from draft import clean_readme, draft_post, draft_thread
from notify import build_daily_messages, build_weekly_messages, send
from score import content_reason, rank, recent_owners, variety_block


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


def prepare(session, repo):
    """README, install command, caption, logo for one repo. None if its README is missing or too short;
    "fail" if GitHub didn't answer."""
    readme = fetch_readme(session, repo["full_name"])
    if readme is None:
        return "fail"
    if len(readme) < config.MIN_README_CHARS:
        print(f"  skip {repo['full_name']}: README too short")
        return None
    handle = fetch_x_handle(session, repo["owner"])
    repo = {**repo, "install": find_install(readme, repo)}
    draft = draft_post(repo, clean_readme(readme), handle)
    return {**repo, **draft, "x_handle": handle, "logo_urls": find_logo_urls(readme, repo)}


STOPPED_EARLY = False   # set when GitHub stopped answering during this run


def pick_repos(session, ranked, count, recent=(), dry_run=False, max_per_category=None):
    """Walk the ranking and prepare up to `count` picks, respecting the variety rules."""
    global STOPPED_EARLY
    STOPPED_EARLY = False
    picks, failures = [], 0
    for repo in ranked:
        if len(picks) >= count:
            break
        why = variety_block(repo, picks, recent, max_per_category)
        if why:
            if dry_run:
                print(f"  skip {repo['full_name']}: {why}")
            continue
        if picks and picks[-1]["used_llm"]:
            time.sleep(config.LLM_DELAY)
        pick = prepare(session, repo)
        if pick == "fail":
            failures += 1
            if failures >= 5:
                print("[warn] 5 README fetches failed in a row (rate limit or outage?), stopping early")
                STOPPED_EARLY = True
                break
            continue
        failures = 0
        if pick:
            picks.append(pick)
            print(f"  pick {repo['full_name']} (score {repo['score']}, {pick['chars']} chars)")
    return picks


def remember(picks, today, now, history_too=True):
    seen = load_json("seen.json", {})
    history = load_json("history.json", [])
    for p in picks:
        seen[p["full_name"].lower()] = today
        if history_too:
            history.append({k: p.get(k) for k in
                            ("full_name", "url", "description", "stars", "language", "license", "install",
                              "category", "score", "text")}
                           | {"date": today})
    cutoff = (now - dt.timedelta(days=60)).date().isoformat()
    save_json("seen.json", seen)
    save_json("history.json", [h for h in history if h["date"] >= cutoff])


def run_daily(dry_run=False, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    today = now.date().isoformat()
    session = gh_session(config.GITHUB_TOKEN)
    seen = load_json("seen.json", {})
    history = load_json("history.json", [])

    candidates = collect(session, now=now)
    ranked = rank(candidates, seen, now=now, verbose=dry_run)
    print(f"{len(candidates)} candidates, {len(ranked)} passed filters")
    picks = pick_repos(session, ranked, config.TOP_N, recent_owners(history, now=now), dry_run)

    send(config.DISCORD_WEBHOOK_URL, build_daily_messages(picks, today, notice=run_notice(picks)), dry_run=dry_run)
    if not dry_run:
        remember(picks, today, now)
    return picks


def run_backfill(dry_run=False, now=None, days=None, count=None):
    """The best repos of the last `days` days (by stars), sent to Discord in one batch."""
    now = now or dt.datetime.now(dt.timezone.utc)
    days = days or config.BACKFILL_DAYS
    count = count or config.BACKFILL_COUNT
    today = now.date().isoformat()
    session = gh_session(config.GITHUB_TOKEN)
    seen = load_json("seen.json", {})

    pages = 3 if count > 10 else 1
    candidates = search_github(session, days_back=days, min_stars=config.BACKFILL_MIN_STARS, topics=[], now=now,
                               pages=pages)
    ranked = rank(candidates, seen, now=now, verbose=dry_run)
    for r in ranked:   # "best of the period": total stars, with the same alternative boost
        r["score"] = round(r["stars"] * (config.ALTERNATIVE_BOOST if r.get("replaces") else 1), 2)
    ranked.sort(key=lambda r: r["score"], reverse=True)
    print(f"backfill: {len(candidates)} candidates from the last {days} days, {len(ranked)} passed filters")
    picks = pick_repos(session, ranked, count, dry_run=dry_run, max_per_category=max(2, -(-count // 3)))

    messages = build_daily_messages(picks, today, notice=run_notice(picks))
    title = (f"## 📚 Backfill: the {len(picks)} best repos of the last {days} days" if picks
             else f"## 📚 Backfill of the last {days} days")
    messages[0]["content"] = messages[0]["content"].replace(f"## 📦 Repo picks for {today}", title, 1)
    send(config.DISCORD_WEBHOOK_URL, messages, dry_run=dry_run)
    if not dry_run:
        remember(picks, today, now, history_too=False)   # never suggested again; weekly thread stays weekly
    return picks


def run_weekly(dry_run=False, now=None, size=5):
    now = now or dt.datetime.now(dt.timezone.utc)
    week_start = (now - dt.timedelta(days=7)).date().isoformat()
    week = [h for h in load_json("history.json", []) if h["date"] >= week_start and not content_reason(h)]
    top = sorted(week, key=lambda h: h["score"], reverse=True)[:size]
    tweets = draft_thread(top) if top else []
    send(config.DISCORD_WEBHOOK_URL, build_weekly_messages(tweets, now.date().isoformat(), items=top),
         dry_run=dry_run)
    return tweets


def run_notice(picks):
    """Everything worth telling about this run: AI failures and GitHub outages."""
    parts = [n for n in (llm_notice(picks),) if n]
    if STOPPED_EARLY:
        parts.append(f"⚠️ **GitHub stopped answering** after {len(picks)} pick(s) (rate limit or outage), so this "
                     "list is short. Nothing is lost: just run the workflow again later.")
    return "\n\n".join(parts) or None


def llm_notice(picks):
    """One clear warning when the AI captions failed, instead of a cryptic line per repo."""
    failed = [p for p in picks if not p.get("used_llm")]
    if not failed:
        return None
    err = (draft.LAST_ERROR or "unknown error").replace("`", "'")[:300]
    return (f"⚠️ **AI captions failed for {len(failed)}/{len(picks)} repos**, so those are template drafts.\n"
            f"Error: `{err}`\nFix: {draft.explain(err)}")


# ---------- doctor ----------

def run_doctor(dry_run=False):
    """Check every part of the bot and post a report with fixes. Changes nothing."""
    import card
    lines, problems = ["## 🩺 repo-hunter doctor"], 0

    def check(ok, text):
        nonlocal problems
        problems += not ok
        lines.append(("✅ " if ok else "❌ ") + text)

    check(bool(config.DISCORD_WEBHOOK_URL), "Discord webhook secret is set" if config.DISCORD_WEBHOOK_URL
          else "DISCORD_WEBHOOK_URL secret is missing")
    check(True, f"Posting {config.TOP_N} drafts a day, repos with {config.MIN_STARS}+ stars from the last "
                f"{config.DAYS_BACK} days" + (f", extra topics: {', '.join(config.TOPICS)}" if config.TOPICS else ""))
    lines.append(f"ℹ️ Handle on images: @{config.ACCOUNT_HANDLE}" if config.ACCOUNT_HANDLE
                 else "ℹ️ No ACCOUNT_HANDLE variable: images show only \"Repo Hunter\"")

    session = gh_session(config.GITHUB_TOKEN)
    try:
        r = session.get(f"{GH_API}/rate_limit", timeout=30)
        r.raise_for_status()
        res = r.json()["resources"]
        check(True, f"GitHub API works ({'with' if config.GITHUB_TOKEN else 'WITHOUT'} token: "
                    f"{res['core']['remaining']}/{res['core']['limit']} calls left this hour)")
    except (requests.RequestException, KeyError, ValueError) as e:
        check(False, f"GitHub API unreachable: `{str(e)[:150]}`")

    ps = draft.providers()
    if not ps:
        check(False, "No AI key: captions use the template.\n   ↳ Fix: " + draft.explain("not set"))
    for p in ps:
        try:
            reply = draft._chat_one(p, [{"role": "user", "content": "Reply with the single word OK."}], 512, 0)
            model = draft._picked.get(p["base"]) or p["model"]
            check(True, f"{p['label'].title()} AI works: model `{model}` at {p['base']} (replied {reply[:20]!r})")
        except Exception as e:
            err = str(e).replace("`", "'")[:250]
            check(False, f"{p['label'].title()} AI failed at {p['base']}: `{err}`\n   ↳ Fix: {draft.explain(err)}")
    if len(ps) == 1:
        lines.append("ℹ️ No backup AI. Optional: add a free Groq key as the LLM2_API_KEY secret.")

    for name, url in (("Hacker News", "https://hn.algolia.com/api/v1/search?tags=show_hn&hitsPerPage=1"),
                      ("Reddit", "https://www.reddit.com/r/opensource/top.json?t=day&limit=1")):
        try:
            r = requests.get(url, headers={"User-Agent": "repo-hunter/1.0 (daily repo digest)"}, timeout=20)
            r.raise_for_status()
            lines.append(f"✅ {name} reachable (extra repo source)")
        except requests.RequestException as e:
            lines.append(f"ℹ️ {name} not reachable from here ({str(e)[:60]}). Not a problem: GitHub search "
                         "still works, you just get fewer candidates.")
    try:
        path = card.render_card({"full_name": "owner/test-repo", "description": "Doctor test image.", "stars": 1,
                                 "language": "Python", "license": "MIT"},
                                os.path.join(tempfile.mkdtemp(), "t.png"), handle="")
        fonts = card._paths.get("Regular") or "built-in"
        check(os.path.getsize(path) > 0, f"Post images render (font: {os.path.basename(fonts)})")
    except Exception as e:
        check(False, f"Post images fail to render: `{str(e)[:150]}`")
    try:
        __import__("cairosvg")
        lines.append("ℹ️ SVG logos supported")
    except (ImportError, OSError):
        lines.append("ℹ️ SVG logos not supported on this machine (PNG/JPG logos still work)")

    seen = load_json("seen.json", {})
    history = load_json("history.json", [])
    last = max((h["date"] for h in history), default="never")
    check(True, f"{len(seen)} repos sent so far, last daily run: {last}")
    lines.append("\n**All good.**" if not problems else f"\n**{problems} problem(s) found.** Fix the ❌ lines above.")
    report = "\n".join(lines)
    send(config.DISCORD_WEBHOOK_URL, [{"content": report[:2000]}], dry_run=dry_run or not config.DISCORD_WEBHOOK_URL)
    return problems


def alert_crash(mode, tb):
    """Post a crash to Discord so it's visible without opening the Actions logs."""
    if not config.DISCORD_WEBHOOK_URL:
        return
    msg = (f"🔥 **repo-hunter crashed** in `{mode}` mode\n```\n{tb[-1500:]}\n```\n"
           "Nothing was marked as sent. Run the `doctor` mode, or paste this to Claude.")
    try:
        requests.post(config.DISCORD_WEBHOOK_URL, json={"content": msg[:2000], "allowed_mentions": {"parse": []}},
                      timeout=30)
    except requests.RequestException:
        pass


def main(argv=None):
    p = argparse.ArgumentParser(description="Find rising GitHub repos and send X drafts to Discord.")
    p.add_argument("mode", nargs="?", default="daily", choices=["daily", "weekly", "doctor", "backfill"])
    p.add_argument("--days", type=int, help="backfill: how many days back (default BACKFILL_DAYS)")
    p.add_argument("--count", type=int, help="backfill: how many repos (default BACKFILL_COUNT)")
    p.add_argument("--dry-run", action="store_true", help="print instead of sending; don't update data/")
    args = p.parse_args(argv)

    if args.mode == "doctor":
        sys.exit(1 if run_doctor(dry_run=args.dry_run) else 0)
    if not args.dry_run and not config.DISCORD_WEBHOOK_URL:
        sys.exit("DISCORD_WEBHOOK_URL is not set. Add it as a secret, or use --dry-run to print instead.")
    try:
        if args.mode == "daily":
            run_daily(dry_run=args.dry_run)
        elif args.mode == "backfill":
            run_backfill(dry_run=args.dry_run, days=args.days, count=args.count)
        else:
            run_weekly(dry_run=args.dry_run)
    except Exception:
        if not args.dry_run:
            alert_crash(args.mode, traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
