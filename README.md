# repo-hunter

Every morning this finds fast-rising GitHub repos and sends the best 5 to Discord. Each one comes with a
ready-to-post image, two short hook captions (A and B) and a copy-paste first reply. You pick one, check the
repo, and post it on X yourself.

```
GitHub search ─┐
Show HN ───────┼─► collect.py ─► score.py ─► draft.py ─► card.py ─► notify.py ─► Discord ─► you post on X
Reddit ────────┘   candidates    filter,      captions    images     webhook
                                 rank, label  (AI)
```

**Cost: $0.** GitHub Actions, the GitHub API, Discord webhooks and the free tiers of Gemini/Groq.

## Modes (Actions → repo-hunter → Run workflow)

| Mode | What it does | When |
|---|---|---|
| `daily` | 5 new repos with images and captions | every day 07:00 (UTC+1), automatically |
| `weekly` | a thread draft of the week's best + a cover image | Sundays 18:00, automatically |
| `doctor` | checks every part of the bot, posts a ✅/❌ report with fixes | whenever something looks wrong |
| `backfill` | the best repos of the last N days (set `days` and `count` in the form) | to fill your queue |

## What you get in Discord, per repo
- the **image** (1600×900): repo name, description, stars, language, license, the repo's **logo** if it has
  one, and its **install command** as a terminal line
- **Option A / Option B**: two short hook captions (1–2 lines)
- 🏷️ the **category** and what it's an **alternative to**, if anything
- a **first reply** block with the GitHub link (and website) to paste under your post
- 🏆 on the best pick of the day, 🚩 when a repo needs a closer look (bought stars, fork farms)
- at the top, a ⚠️ banner explaining any problem (AI failing, GitHub not answering) and how to fix it

## Setup
Secrets (Settings → Secrets and variables → Actions → Secrets):
- `DISCORD_WEBHOOK_URL`: Discord → Server Settings → Integrations → Webhooks → Copy Webhook URL
- `LLM_API_KEY`: a free Gemini key from aistudio.google.com → Get API key
- `LLM2_API_KEY` *(optional backup AI)*: a free Groq key from console.groq.com. Used when Gemini fails.

Variables (same page → Variables), all optional:

| Variable | Default | What it does |
|---|---|---|
| `ACCOUNT_HANDLE` | none | your X handle, printed on the images |
| `TOP_N` | 5 | drafts per day |
| `MIN_STARS` | 100 | minimum stars for the daily search |
| `TOPICS` | none | extra searches, e.g. `self-hosted,llm,cli` |
| `NICHE` | devs, students, AI builders | who the captions are written for |
| `LLM_MODEL` | `gemini-flash-latest` | remove it to let the bot pick a model itself |
| `SKIP_GAME_PORTS` | true | skip GTA/console ports, emulators and game recompilations |
| `MAX_PER_CATEGORY` | 2 | variety: max repos of one category per day |
| `OWNER_COOLDOWN_DAYS` | 3 | don't feature the same owner again within N days |
| `ALTERNATIVE_BOOST` | 1.3 | score bonus for "alternative to X" repos |

More in `config.py`.

## How repos are chosen
- **Dropped:** already sent; archived; forks; no license; descriptions under 3 words; inactive for 30 days;
  READMEs under 500 characters; awesome-lists, dotfiles, homework; spam/malware keywords; non-English;
  re-uploads/mirrors of other repos; game ports and console emulators.
- **Score:** stars per day since creation. +50% per Hacker News/Reddit mention, +30% for "alternative to X".
- **Variety:** one repo per owner per day, no owner again within 3 days, max 2 per category.

## Troubleshooting
Run the `doctor` mode first. Its report says what's wrong and how to fix it. If a run crashes, the error is
also posted to Discord.

## Run locally
```bash
pip install -r requirements.txt
python main.py daily --dry-run            # prints everything, sends nothing, changes nothing
python main.py doctor --dry-run
python main.py backfill --dry-run --days 30 --count 10
pip install pytest && python -m pytest -q
```
