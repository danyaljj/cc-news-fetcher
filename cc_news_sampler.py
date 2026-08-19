#!/usr/bin/env python3
"""
Build a news-article corpus from CommonCrawl. Two steps, one script:

  survey  Estimate how many captures CC holds per news site per year (no content
          download) and plot a hits-per-year heatmap — size the corpus first.
  fetch   Randomly sample N pages per site per dump, byte-range-fetch the archived
          HTML from CC, extract clean main text with trafilatura, write JSONL.

Everything is read from CommonCrawl's archives (data.commoncrawl.org); the live
news sites are never contacted.

Usage:
    python3 cc_news_sampler.py survey --lo 2013 --hi 2022
    python3 cc_news_sampler.py fetch  --per-dump 2 --out news_corpus.jsonl
    python3 cc_news_sampler.py fetch  --sites www.nytimes.com/* www.theguardian.com/*
"""
import argparse, io, json, os, random, sys, time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from warcio.archiveiterator import ArchiveIterator
import trafilatura
from tqdm import tqdm

CC_DATA = "https://data.commoncrawl.org"
COLLINFO = "https://index.commoncrawl.org/collinfo.json"
UA = "cc-news-sampler/1.0 (research; contact danielk@jhu.edu)"

# Fallback news sites (CDX url patterns) if news_sites.txt is absent.
DEFAULT_SITES = [
    "www.nytimes.com/*",
    "www.theguardian.com/*",
    "www.bbc.com/*",
    "www.washingtonpost.com/*",
    "www.reuters.com/*",
]

session = requests.Session()
session.headers.update({"User-Agent": UA})


# --------------------------------------------------------------------------- #
# Shared CommonCrawl helpers
# --------------------------------------------------------------------------- #
def get(url, **kw):
    """GET with a few retries (CC index/data can 503 under load)."""
    for attempt in range(6):
        try:
            r = session.get(url, timeout=60, **kw)
            if r.status_code in (200, 206):   # 206 = partial (byte-range WARC fetch)
                return r
            if r.status_code in (404, 416):
                return r
        except requests.RequestException:
            pass
        time.sleep(2 * (attempt + 1))
    return None


def dumps_in_range(lo=2013, hi=2024):
    info = session.get(COLLINFO, timeout=60).json()
    out = []
    for c in info:
        cid = c["id"]                       # e.g. CC-MAIN-2024-42
        yr = int(cid.split("-")[2])
        if lo <= yr <= hi:
            out.append((cid, c["cdx-api"]))
    out.sort(key=lambda x: x[0])
    return out


def one_dump_per_year(dumps):
    """Keep the first dump seen for each year (dumps are sorted ascending)."""
    seen, out = set(), []
    for cid, cdx in dumps:
        yr = int(cid.split("-")[2])
        if yr not in seen:
            seen.add(yr)
            out.append((cid, cdx))
    return out


def load_sites_file(path):
    """One CDX url-pattern per line; '#' comments and blank lines ignored."""
    sites = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                sites.append(line)
    return sites


def resolve_sites(args):
    """Resolve site list: --sites > --sites-file > news_sites.txt > builtin."""
    if args.sites:
        return args.sites
    if args.sites_file:
        return load_sites_file(args.sites_file)
    if os.path.exists("news_sites.txt"):
        return load_sites_file("news_sites.txt")
    return DEFAULT_SITES


# --------------------------------------------------------------------------- #
# survey: estimate capture counts (no content download)
# --------------------------------------------------------------------------- #
def count_records(text):
    """Count valid capture records in a CDX page body (ignore 'No Captures' msg)."""
    n = 0
    for line in text.splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if "timestamp" in rec:      # a real capture, not an error message
            n += 1
    return n


def estimate_hits(cdx_api, site):
    """Estimate # of status-200 text/html captures for `site` in one dump.

    Cheap: 2 index requests. Read the CDX page count and multiply by the record
    count on page 0. Self-corrects to 0 for sites a dump did not capture.
    """
    base = {
        "url": site,
        "output": "json",
        "filter": ["=status:200", "=mime:text/html"],
    }
    r = get(cdx_api, params={**base, "showNumPages": "true"})
    if r is None or r.status_code != 200:
        return None                 # index error -> unknown (distinct from 0)
    try:
        num_pages = json.loads(r.text).get("pages", 0)
    except Exception:
        return None
    if num_pages == 0:
        return 0
    r0 = get(cdx_api, params={**base, "page": 0})
    if r0 is None or r0.status_code != 200:
        return None
    per_page = count_records(r0.text)
    if per_page == 0:
        return 0
    return per_page * num_pages if num_pages > 1 else per_page


def cmd_survey(args):
    sites = resolve_sites(args)
    dumps = dumps_in_range(args.lo, args.hi)
    if not args.all_dumps:
        dumps = one_dump_per_year(dumps)
    print(f"{len(sites)} sites x {len(dumps)} dumps ({args.lo}-{args.hi}) "
          f"= {len(sites) * len(dumps)} cells", file=sys.stderr)

    # Fan out every (site, dump) cell across a thread pool with a live progress bar.
    rows = []
    tasks = [(site, cid, cdx) for cid, cdx in dumps for site in sites]
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(estimate_hits, cdx, site): (site, cid)
                for site, cid, cdx in tasks}
        bar = tqdm(total=len(futs), desc="surveying", unit="cell")
        running_total = 0
        for fut in as_completed(futs):
            site, cid = futs[fut]
            try:
                est = fut.result()
            except Exception:
                est = None
            year = int(cid.split("-")[2])
            rows.append({"site": site, "dump": cid, "year": year, "hits": est})
            if est:
                running_total += est
            bar.update(1)
            bar.set_postfix_str(f"~{running_total:,} captures so far")
        bar.close()

    _write_survey_outputs(rows, sites, args)


def _write_survey_outputs(rows, sites, args):
    import pandas as pd
    df = pd.DataFrame(rows)
    df.to_csv(args.out_csv, index=False)
    print(f"wrote {args.out_csv} ({len(df)} rows)", file=sys.stderr)

    # Aggregate to a site x year matrix of estimated hits.
    piv = (df.dropna(subset=["hits"])
             .pivot_table(index="site", columns="year", values="hits",
                          aggfunc="sum", fill_value=0))
    piv = piv.reindex([s for s in sites if s in piv.index])   # input file order

    per_site = piv.sum(axis=1).sort_values(ascending=False)
    print("\nTop 15 sites by estimated total captures:", file=sys.stderr)
    for site, tot in per_site.head(15).items():
        print(f"  {int(tot):>12,}  {site}", file=sys.stderr)
    print(f"\nGRAND TOTAL estimated captures: {int(per_site.sum()):,}", file=sys.stderr)

    _plot_heatmap(piv, args.out_png)


def _plot_heatmap(piv, out_png):
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LogNorm

    data = piv.to_numpy(dtype=float)
    data_masked = np.ma.masked_where(data <= 0, data)
    fig, ax = plt.subplots(figsize=(max(8, 0.5 * piv.shape[1] + 4),
                                    max(6, 0.28 * piv.shape[0] + 2)))
    cmap = plt.cm.viridis.copy()
    cmap.set_bad("#eeeeee")         # empty cells in light gray
    vmin = max(1, np.nanmin(data_masked)) if data_masked.count() else 1
    im = ax.imshow(data_masked, aspect="auto", cmap=cmap,
                   norm=LogNorm(vmin=vmin, vmax=max(vmin + 1, data.max())))
    ax.set_xticks(range(piv.shape[1]))
    ax.set_xticklabels(piv.columns, rotation=45, ha="right", fontsize=8)
    ax.set_yticks(range(piv.shape[0]))
    ax.set_yticklabels([s.replace("/*", "") for s in piv.index], fontsize=7)
    ax.set_xlabel("year")
    ax.set_title("Estimated CommonCrawl captures per news site per year")
    fig.colorbar(im, ax=ax, label="estimated captures (log scale)")
    fig.tight_layout()
    fig.savefig(out_png, dpi=130)
    print(f"wrote {out_png}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# fetch: sample + download + extract clean text
# --------------------------------------------------------------------------- #
def sample_records(cdx_api, url_pattern, n, rng, max_pages_probe=8):
    """Randomly sample n status-200 HTML capture records for url_pattern."""
    base = {
        "url": url_pattern,
        "output": "json",
        "filter": ["=status:200", "=mime:text/html"],
    }
    # How many index pages exist for this query?
    r = get(cdx_api, params={**base, "showNumPages": "true"})
    if r is None or r.status_code != 200:
        return []
    try:
        num_pages = json.loads(r.text).get("pages", 0)
    except Exception:
        num_pages = 0
    if num_pages == 0:
        return []

    # Pull a random subset of index pages and gather candidate records.
    page_ids = list(range(num_pages))
    rng.shuffle(page_ids)
    candidates, seen = [], set()
    for pid in page_ids[:max_pages_probe]:
        r = get(cdx_api, params={**base, "page": pid})
        if r is None or r.status_code != 200:
            continue
        for line in r.text.splitlines():
            try:
                rec = json.loads(line)
            except Exception:
                continue
            key = rec.get("urlkey")
            if key in seen:
                continue
            seen.add(key)
            candidates.append(rec)
        if len(candidates) >= n * 20:      # enough to sample from
            break
    rng.shuffle(candidates)
    return candidates[: n * 4]             # oversample; some fetches may fail


def fetch_clean_text(rec):
    """Range-GET the WARC record for one capture and extract main text."""
    fn, off, length = rec["filename"], int(rec["offset"]), int(rec["length"])
    headers = {"Range": f"bytes={off}-{off + length - 1}"}
    r = get(f"{CC_DATA}/{fn}", headers=headers)
    if r is None or r.status_code not in (200, 206):
        return None
    try:
        for record in ArchiveIterator(io.BytesIO(r.content)):
            if record.rec_type != "response":
                continue
            html = record.content_stream().read().decode("utf-8", "replace")
            text = trafilatura.extract(
                html, include_comments=False, include_tables=False,
                favor_precision=True,
            )
            if not text or len(text.split()) < 40:   # skip near-empty pages
                return None
            meta = trafilatura.extract_metadata(html)
            return {
                "text": text,
                "title": getattr(meta, "title", None) if meta else None,
            }
    except Exception:
        return None
    return None


def cmd_fetch(args):
    sites = resolve_sites(args)
    rng = random.Random(args.seed)
    dumps = dumps_in_range(args.lo, args.hi)
    print(f"{len(sites)} sites x {len(dumps)} dumps in {args.lo}-{args.hi}",
          file=sys.stderr)

    total = 0
    per_site_kept = defaultdict(int)     # enforce --max-per-site across all dumps
    with open(args.out, "w") as fh:
        # Outer progress bar over dumps (years), inner bar over sites.
        for cid, cdx_api in tqdm(dumps, desc="dumps", unit="dump", position=0):
            site_bar = tqdm(sites, desc=cid, unit="site", position=1, leave=False)
            for site in site_bar:
                if per_site_kept[site] >= args.max_per_site:
                    continue
                site_bar.set_postfix_str(site.replace("/*", ""))
                cands = sample_records(cdx_api, site, args.per_dump, rng)
                kept = 0
                for rec in cands:
                    if kept >= args.per_dump or \
                            per_site_kept[site] >= args.max_per_site:
                        break
                    doc = fetch_clean_text(rec)
                    if not doc:
                        continue
                    fh.write(json.dumps({
                        "id": f"{cid}:{rec['digest']}",
                        "dump": cid,
                        "site": site,
                        "url": rec["url"],
                        "timestamp": rec["timestamp"],
                        "title": doc["title"],
                        "text": doc["text"],
                    }, ensure_ascii=False) + "\n")
                    fh.flush()
                    kept += 1
                    per_site_kept[site] += 1
                    total += 1
                tqdm.write(f"{cid} {site}: kept {kept}/{args.per_dump} "
                           f"(from {len(cands)} candidates)")
            site_bar.close()
    print(f"DONE: {total} docs -> {args.out}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def add_common_args(p):
    p.add_argument("--sites", nargs="+", default=None,
                   help="CDX url patterns, space-separated "
                        "(e.g. 'www.nytimes.com/*' 'www.theguardian.com/*'). "
                        "Overrides --sites-file when given.")
    p.add_argument("--sites-file", default=None,
                   help="file with one CDX url-pattern per line "
                        "(default: news_sites.txt if present, else a small builtin list)")
    p.add_argument("--lo", type=int, default=2013,
                   help="earliest dump year to include, inclusive (default: 2013)")
    p.add_argument("--hi", type=int, default=2022,
                   help="latest dump year to include, inclusive (default: 2022; "
                        "recent dumps block many news crawlers via robots.txt)")


def main():
    ap = argparse.ArgumentParser(
        description="Survey and sample a news corpus from CommonCrawl.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("survey", help="estimate capture counts per site per year "
                                       "(no download) + heatmap")
    add_common_args(sp)
    sp.add_argument("--workers", type=int, default=3,
                    help="concurrent index requests (keep low; index.commoncrawl.org "
                         "refuses connections if hammered) (default: 3)")
    sp.add_argument("--all-dumps", action="store_true",
                    help="survey every dump; default samples ONE dump per year "
                         "(enough for a per-year histogram, ~10x fewer requests)")
    sp.add_argument("--out-csv", default="survey.csv",
                    help="per-site/per-dump counts output (default: survey.csv)")
    sp.add_argument("--out-png", default="survey.png",
                    help="hits-per-year heatmap output (default: survey.png)")
    sp.set_defaults(func=cmd_survey)

    fp = sub.add_parser("fetch", help="sample pages, download, extract clean text -> JSONL")
    add_common_args(fp)
    fp.add_argument("--per-dump", type=int, default=2,
                    help="pages to sample per site per dump (default: 2)")
    fp.add_argument("--max-per-site", type=int, default=200_000,
                    help="stop after keeping this many pages per site across all dumps "
                         "(default: 200000)")
    fp.add_argument("--out", default="news_corpus.jsonl",
                    help="output JSONL path (default: news_corpus.jsonl)")
    fp.add_argument("--seed", type=int, default=42,
                    help="RNG seed for reproducible sampling (default: 42)")
    fp.set_defaults(func=cmd_fetch)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
