# Revolution Force schedule tracker

A free, automatic schedule page for **Markham Revolution Force** (OVA 15U & TLS Girls).

- Checks the OVA Tourney app on **timu.ca** every 3 hours, and hourly on weekends.
- Finds every division where the team is seeded, then pulls the date, venue, address, match times, courts, opponents, scores and final placement.
- Publishes a phone-friendly page with three styles (Court, Clean, Night).
- Provides a **calendar feed** parents subscribe to once (Google, Apple, Outlook). Time changes flow in automatically, with a reminder the night before.
- Includes share buttons for **WhatsApp**, **email** and **copy message**.
- Shows the team's current **OVA ranking** from ontariovolleyball.org/girls-team-rankings, plus links to the season's division-split PDFs.

Hosting is free on GitHub Pages. You don't need a server or an API key.

---

## Setup (about 10 minutes, one time)

1. **Create a GitHub account** at [github.com](https://github.com) if you don't have one.
2. **Create a repository.** Click **New repository**, name it `revolution-tracker`, set it to **Public** (free Pages hosting requires public), then **Create**.
3. **Upload the files.** On the new repo page, click **uploading an existing file**. Drag in *everything inside* the unzipped folder, including the hidden `.github` folder, then **Commit changes**.
   - On a Mac, press `Cmd+Shift+.` in Finder to show hidden folders.
   - Or install [GitHub Desktop](https://desktop.github.com) and publish the folder from there, which handles hidden folders automatically.
4. **Turn on Pages.** Go to **Settings → Pages → Build and deployment → Source** and choose **GitHub Actions**.
5. **Allow the bot to save its cache.** Go to **Settings → Actions → General → Workflow permissions**, choose **Read and write permissions**, and click **Save**.
6. **Set your address.** Open `config.yaml` on GitHub and click the pencil icon. Replace `thiagomides` in `base_url`, then commit.
7. **Run it.** Go to the **Actions** tab, open **Update schedule**, and click **Run workflow**. After about 2 minutes, your page is live at:
   `https://thiagomides.github.io/revolution-tracker/`

**Share that one link** with parents. Everything else, including calendar subscriptions and WhatsApp sharing, happens on the page.

---

## Everyday use

| You want to… | Do this |
|---|---|
| Change the default look | In `config.yaml`, set `theme:` to `sport`, `clean` or `night` |
| Track another competition | In `config.yaml`, add it to `timu.divisions`, e.g. `16UG` |
| Add a non-timu event (exhibition, Ontario Championships) | Add it to `data/tournaments.yaml` (an example is inside the file) |
| Force an update now | Go to **Actions**, open **Update schedule**, and click **Run workflow** |
| New season | In `config.yaml`, update `season_start` and `show_results_from` |

Every edit on GitHub rebuilds the page automatically.

## Email alerts (optional)

To email parents when times, venues or results change:

1. In `config.yaml`, set `alerts.email_enabled: true` and add addresses under `alerts.to`.
2. In the repo, go to **Settings → Secrets and variables → Actions** and add these secrets: `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER` and `SMTP_PASS`.
   - For Gmail, use `smtp.gmail.com` and port `587`, with an [App Password](https://myaccount.google.com/apppasswords), not your normal password.

---

## How it works

```
GitHub Actions (every 3 h; hourly on weekends)
  └─ tracker/build.py
       ├─ tracker/timu.py   timu.ca/ova/index.php   → list of 15U/TLS Girls divisions + dates
       │                    scoreboards/schedule.php → venue, address, start time, seeds, time×court grid
       │                    scoreboards/results.php  → scores, playoff rounds → placement
       ├─ tracker/ova_events.py OVA events calendar → our regular-season TLS D1 cups → our tier, day, venue, timu link
       ├─ tracker/rankings.py ontariovolleyball.org/girls-team-rankings → our rank, table by table
       ├─ data/tournaments.yaml  (hand-entered extras)
       ├─ tracker/aes.py         (optional, Ontario Championships on AES)
       └─ writes site/: index.html · schedule.ics · events/*.ics · data.json · digest.txt
```

**Only our tournaments.** The OVA events calendar lists every competition. The tool keeps only regular-season cups whose title matches `ova_events.titles` in `config.yaml` (default: TLS Girls Division 1). It opens each event page and shows only the tier Revolution Force is in, such as "Trillium I", with its day and venue. Exhibitions, Non-OVA events and other tiers are never shown. Until OVA posts the team splits, about a month before each event, the page shows "Sat or Sun" and "Tier: posted ~1 month before".

**Seeding timing.** OVA usually posts divisions about a week before each event. Until then, the page shows "Waiting for OVA seeding" along with the upcoming 15U and TLS dates. As soon as Revolution Force appears in a division, the tournament is added.

**Being polite.** The tool pauses 0.5 seconds between requests. It never re-downloads finished events, because `data/timu_cache.json` remembers them.

**Error handling.**
- Parser tests run before every publish. If timu.ca changes its layout, the run stops and the last good page stays up. GitHub also emails you that the run failed.
- A failed page load becomes a small notice on the page, and the tool retries on the next run.
- The tool never publishes an empty schedule.
- Calendar events have stable IDs, so changes update the existing entries instead of creating duplicates.

**Rankings.** OVA posts new rankings after each regular-season event. The page shows the newest table that lists the team, and falls back to last season's final ranking until the first 2026-27 ranking appears. The OVA site sometimes blocks automated visits (error 403). When that happens, the page keeps the last saved ranking, shows the date it was checked, and retries on the next run.

**Limits.**
- Pool-play times after the first match show as "then", because timu lists the matches in order without clock times.
- Playoff times aren't known until pool play ends.
- Google Calendar refreshes subscribed feeds only every several hours. For same-day changes, rely on the page, email alerts or live timu.

**Run locally**
```bash
pip install -r requirements.txt
python -m pytest -q tests        # parser tests on saved timu pages
python -m tracker.build          # builds site/ (OFFLINE=1 uses cached data only)
```

*This is an unofficial page run by parents. The official source is [timu.ca/ova](https://timu.ca/ova/index.php) and [ontariovolleyball.org](https://www.ontariovolleyball.org).*
