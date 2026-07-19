# Zurich UAE Fund Performance Dashboard

Self-updating static dashboard for Zurich UAE fund-centre funds.

## What It Shows

- Current live-fund list from the Zurich UAE fund centre powered by FE fundinfo.
- Small performance charts for validated price histories.
- Best, mediocre, and worst groupings ranked by total return over the available FE fundinfo price-history window.
- Search, group, and sort controls in the browser.

## Files

- `index.html` renders the dashboard.
- `data/funds.json` contains the latest fund data used by the dashboard.
- `scripts/update_zurich_funds.py` refreshes the data from Zurich/FE fundinfo.
- `.github/workflows/refresh.yml` runs the refresh automatically on GitHub Actions.

## GitHub Pages Setup

1. Create a GitHub repository and upload these files.
2. In the repository, open **Settings > Pages**.
3. Set **Build and deployment** to **Deploy from a branch**.
4. Select the default branch and `/ (root)`.
5. Save. GitHub will provide the public dashboard URL.

## Refresh Schedule

The workflow runs Monday to Friday at `03:20 UTC` and can also be started manually from **Actions > Refresh Zurich fund data > Run workflow**.

## Data Note

The FE fundinfo Download Tool did not return 5-year price rows during the initial build, so the dashboard uses the validated 3-year or available price-history window.
