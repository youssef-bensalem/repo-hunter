# repo-hunter

Every morning this finds fast-rising GitHub repos, drafts an X post for each one, and sends the
top 5 to a Discord channel. You pick 1–2, check them, and post them yourself. On Sundays it also
drafts a thread from the week's best picks.

```
GitHub search ─┐
Show HN ───────┼─► collect.py ─► score.py ─► draft.py ─► notify.py ─► Discord ─► you post on X
Reddit ────────┘   (candidates)  (filter +    (LLM or     (webhook)
                                  rank)        template)
```

**Cost: $0.** GitHub Actions is free (unlimited on public repos, ~3 of your 2,000 free minutes a
day on private ones), and the GitHub API, Discord webhooks and the Gemini free tier are all free.

## Setup (about 10 minutes)

1. **Create a GitHub repo** and push these files to it.
2. **Discord webhook:** in your server, open *Server Settings → Integrations → Webhooks → New Webhook*,
   choose the channel, then *Copy Webhook URL*.
3. **LLM key (optional but recommended):** create a free Gemini API key in Google AI Studio.
   Without a key the bot still works and uses simple template drafts.
4. **Add the secrets** in the repo under *Settings → Secrets and variables → Actions → New repository secret*:
   - `DISCORD_WEBHOOK_URL`
   - `LLM_API_KEY`
5. **Test it:** open the *Actions* tab → **repo-hunter** → *Run workflow*. Drafts should show up in
   Discord within about a minute.

After that it runs every day at 07:00 (UTC+1) and on Sundays at 18:00 for the thread.
To change the times, edit the `cron` lines in `.github/workflows/repo-hunter.yml` (they're in UTC).

## Daily routine (~15 min)

1. Open Discord and pick 1–2 drafts.
2. **Open the repo and skim the README and code.** A 🚩 means stars are coming in unusually fast,
   which is common with bought stars and malware-bait repos (fake game ports, "cracked" tools).
   Never post a repo you haven't opened.
3. Rewrite the hook in your own words, then post it with the preview image
   (`opengraph.githubassets.com/...` from the embed) or your own screenshot.
4. Put the GitHub link in the **first reply**, not in the post itself.
5. If the post shows `by @handle`, keep it: maintainers often repost.

## Settings

Set these as repository **variables** (*Settings → Secrets and variables → Actions → Variables*).
Anything left unset uses the default from `config.py`.

| Variable | Default | What it does |
|---|---|---|
| `NICHE` | devs, students, AI/ML builders | Tells the LLM who the posts are for |
| `TOPICS` | *(none)* | Extra searches, comma-separated, e.g. `self-hosted,llm,cli` |
| `MIN_STARS` | `100` | Minimum stars for the main search |
| `TOP_N` | `5` | Drafts per day |
| `LLM_MODEL` | `gemini-flash-latest` | Model name |
| `LLM_BASE_URL` | Gemini's OpenAI-compatible URL | Change this to use another provider |

More settings in `config.py`: `DAYS_BACK`, `SUBREDDITS`, `MAX_INACTIVE_DAYS`, `ENGLISH_ONLY`,
the exclude patterns, and `SUSPICIOUS_STARS_PER_DAY`.

**Other free LLMs:** any OpenAI-compatible API works. For Groq, set `LLM_BASE_URL` to
`https://api.groq.com/openai/v1` and `LLM_MODEL` to a model from their docs. If Gemini returns a
"model not found" error, set `LLM_MODEL` to a current model name from the Gemini docs.

## How repos are filtered and ranked

- **Dropped:** already sent, archived, forks, no license, no description, no push in 30 days,
  a README under 500 characters, awesome-lists, dotfiles, homework, spam/malware keywords, and
  non-English descriptions.
- **Score** = stars per day since the repo was created. A repo that is also on Show HN or Reddit
  gets +50% per community source, and its age is capped at 30 days, so an older repo that's
  suddenly being discussed can still rank.
- Sent repos are recorded in `data/seen.json` (the workflow commits it back), so they never repeat.

## Run locally

```bash
pip install -r requirements.txt
python main.py daily --dry-run          # prints drafts, sends nothing, changes nothing
python main.py weekly --dry-run
pip install pytest && python -m pytest -q
```

Set `GITHUB_TOKEN` (any personal token, no scopes needed) to avoid the 60-requests/hour
anonymous limit, and `LLM_API_KEY` for LLM drafts.
