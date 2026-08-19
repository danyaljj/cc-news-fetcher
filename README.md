# cc-news-fetcher

Sample and download news-article text from [CommonCrawl](https://commoncrawl.org/)
across many years, for building an IR / retrieval corpus. Everything is fetched
from **CommonCrawl's archives** (`data.commoncrawl.org`) — the live news sites are
never contacted, so pages that have since changed or disappeared are still recoverable.

One script, `cc_news_sampler.py`, with two subcommands:

| Subcommand | What it does |
|---|---|
| `survey` | **Estimate first.** Counts how many captures CommonCrawl holds per news site per year (no content download) and plots a hits-per-year heatmap, so you can size the corpus before crawling. |
| `fetch` | **Then crawl.** Randomly samples N pages per site per dump, byte-range-fetches the archived HTML from CC, extracts clean main text with [trafilatura](https://trafilatura.readthedocs.io/) (ads/nav/boilerplate stripped), and writes one JSONL doc per page. |

## Install

```bash
pip install requests warcio trafilatura tqdm matplotlib pandas
```

## 1. Survey volume (recommended first step)

```bash
python3 cc_news_sampler.py survey --sites-file news_sites.txt --lo 2013 --hi 2022 \
    --out-csv survey.csv --out-png survey.png
```

By default it samples **one dump per year** (enough for a per-year histogram, ~10×
fewer index requests). Add `--all-dumps` for every dump. Output: a `survey.csv` of
`site,dump,year,hits` plus `survey.png`, a site × year heatmap of estimated captures.

## 2. Crawl the text

```bash
# one site, quick test
python3 cc_news_sampler.py fetch --sites 'www.theguardian.com/*' \
    --per-dump 2 --lo 2018 --hi 2020 --out guardian.jsonl

# all sites in news_sites.txt, one file per site, capped at 200k pages/site
./run_sampler.sh
```

Each output line is:

```json
{"id": "CC-MAIN-2020-05:DIGEST", "dump": "CC-MAIN-2020-05",
 "site": "www.theguardian.com/*", "url": "https://www.theguardian.com/...",
 "timestamp": "20200129...", "title": "...", "text": "clean article body ..."}
```

## Notes & gotchas

- **`index.commoncrawl.org` is aggressively rate-limited.** Too many concurrent
  requests get your IP temporarily blocked at the TCP level (connection refused,
  not HTTP 503) for several minutes. Keep index concurrency low (`--workers 3`).
  The content host `data.commoncrawl.org` is not throttled the same way.
- **Recent dumps (2023+) have few news captures** — many outlets block CommonCrawl's
  crawler via `robots.txt`. Prefer pre-2023 dumps (the defaults use `--hi 2022`).
- Byte-range WARC fetches return **HTTP 206** — that's expected/success.
- `news_sites.txt` holds the site list (one CDX url-pattern per line). Edit to taste.

## Files

- `cc_news_sampler.py` — the tool: `survey` (volume + heatmap) and `fetch` (download + extract)
- `run_sampler.sh` — bash wrapper: `fetch` every site in `news_sites.txt`, one file each
- `news_sites.txt` — ~100 news-site CDX patterns
