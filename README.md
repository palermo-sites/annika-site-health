# annika-site-health

Health dashboard of the website system **annika** (annikapalermo.com): https://palermo-sites.github.io/annika-site-health/

Modelled on the agentic platform kit's per-system health dashboard, which lives in a repository of its own beside the
system (website-platform-kit decision 0008). The site's own Pages serves the website and `annika-site-uat` serves UAT,
so the dashboard has this third repository and never appears on the public site.

- **Structure:** site repository, UAT repository, environments UAT then prod with their addresses and hosts, workflows,
  domain status.
- **Health:** each environment's node probed over HTTPS (status, time, noindex), uptime over 7 days, last deploy and
  any deploy in flight, deploys and change failure rate over 7 days, whether `main` is green, what happened recently.
- **Delivery:** today so far, 7 and 30 days and Rolling 12 weeks from the site's `scorecard-data` branch (delivered,
  lead time, deploys per day, change failure rate), with a link to the scorecard report.

Workflow `health` runs every 30 minutes (and on push, and by hand): `scripts/health.py` reads `health.config.json`,
writes `index.html`, `health.json` and `history.json`, and deploys them to Pages. It uses only the run's own token and
public data; the probe history is carried forward by reading the previously published `history.json`, so nothing is
committed. Template: `website-platform-kit` `.claude/skills/website-system/templates/health/`.
