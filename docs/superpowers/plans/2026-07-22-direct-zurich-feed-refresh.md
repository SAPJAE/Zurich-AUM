# Direct Zurich Feed Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair the Zurich AUM scheduled refresh so it updates only from the live Zurich/FE fund centre, without Excel fallback.

**Architecture:** Keep the existing single Python scraper and GitHub Actions workflow. Replace the brittle DOM-only fund-list extraction with direct feed-aware extraction and diagnostics, while preserving the existing per-fund price-history download validation.

**Tech Stack:** Python 3.12, Playwright Chromium, GitHub Actions, existing `scripts/update_zurich_funds.py`, existing encrypted dashboard data flow.

## Global Constraints

- No Excel fallback or manual downloaded file dependency.
- Only commit refreshed data when direct Zurich/FE scraping finds the expected live fund list.
- If the direct feed returns no funds or too few funds, fail loudly and leave existing dashboard data untouched.
- Keep the public dashboard password/encryption model unchanged.

---

### Task 1: Add Direct Feed Diagnostics And Robust List Extraction

**Files:**
- Modify: `scripts/update_zurich_funds.py`

**Interfaces:**
- Consumes: existing `SOURCE_URL`, `DOWNLOAD_URL`, `scrape_history(page, fund)`, and `classify_funds(raw_funds)`.
- Produces: `scrape_live_funds(page)` returning a list of dictionaries with `name`, `fundCentreCode`, `productType`, `price`, `priceDate`, and `changePct`.

- [ ] **Step 1: Reproduce the current failure**

Run:

```bash
python scripts/update_zurich_funds.py --output data/funds.raw.test.json
```

Expected before the fix: failure with `RuntimeError: No funds were found on the Zurich/FE fund centre page.`

- [ ] **Step 2: Add diagnostics around the live page**

Update `scrape_live_funds(page)` to collect:

```python
print(f"Loaded {page.url}", flush=True)
print(f"Page title: {await page.title()}", flush=True)
```

When zero funds are found, also print a short body preview and counts for `tr`, `script`, `iframe`, and likely fund-code text matches.

- [ ] **Step 3: Add robust direct extraction**

Extend `scrape_live_funds(page)` to try these sources in order:

```python
funds = await extract_funds_from_dom_table(page)
if not funds:
    funds = await extract_funds_from_embedded_page_state(page)
if not funds:
    funds = await extract_funds_from_download_links(page)
```

Each extractor must return the same fund dictionary shape already consumed by `scrape_history()`.

- [ ] **Step 4: Keep strict validation**

If extraction returns fewer than `150` funds, raise:

```python
raise RuntimeError(f"Only {len(funds)} funds were found on the Zurich/FE fund centre page.")
```

- [ ] **Step 5: Run local refresh**

Run:

```bash
python scripts/update_zurich_funds.py --output data/funds.raw.test.json
```

Expected after the fix: writes a JSON file with about `168` funds and no scraper-list failure.

### Task 2: Verify Workflow Readiness

**Files:**
- Modify: `.github/workflows/refresh.yml` only if diagnostics need environment flags.

**Interfaces:**
- Consumes: patched `scripts/update_zurich_funds.py`.
- Produces: GitHub Actions run that reaches `Encrypt fund data`.

- [ ] **Step 1: Validate script syntax**

Run:

```bash
python -m py_compile scripts/update_zurich_funds.py
```

Expected: no output and exit code `0`.

- [ ] **Step 2: Run a no-commit local generation**

Run:

```bash
python scripts/update_zurich_funds.py --output data/funds.raw.test.json
```

Expected: `Wrote data/funds.raw.test.json with ... funds`.

- [ ] **Step 3: Remove local test output**

Run:

```bash
rm data/funds.raw.test.json
```

Expected: repo diff contains only the scraper/workflow fix.

- [ ] **Step 4: Commit and push**

Run:

```bash
git add scripts/update_zurich_funds.py .github/workflows/refresh.yml
git commit -m "Fix direct Zurich fund refresh"
git push
```

Expected: changes pushed to `main`.

- [ ] **Step 5: Trigger the workflow manually**

Use GitHub Actions `Run workflow` on `Refresh Zurich fund data`.

Expected: the workflow does not fail with `No funds were found`; if Zurich blocks the runner, diagnostics show the specific page state.
