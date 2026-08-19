#!/usr/bin/env bash
#
# Crawl the top-N news sites from CommonCrawl, one site at a time, capping each
# site at --max-per-site pages. One JSONL file per site under $OUTDIR so a crash
# only loses the site in flight (already-finished sites are skipped on rerun).
#
# Progress is two-level:
#   - this script prints  [i/N] <site>   as it moves across sites (per-news)
#   - the Python sampler shows a tqdm bar over dumps/years         (per-year)
#
# Usage:
#   ./run_sampler.sh                        # all sites in news_sites.txt
#   MAX_PER_SITE=200000 LO=2013 HI=2022 ./run_sampler.sh
#   PER_DUMP=5 OUTDIR=corpus ./run_sampler.sh news_sites.txt
#
set -euo pipefail

SITES_FILE="${1:-news_sites.txt}"
OUTDIR="${OUTDIR:-corpus}"
MAX_PER_SITE="${MAX_PER_SITE:-200000}"
PER_DUMP="${PER_DUMP:-50}"
LO="${LO:-2013}"
HI="${HI:-2022}"
SCRIPT="$(dirname "$0")/cc_news_sampler.py"

mkdir -p "$OUTDIR"

# Read non-comment, non-blank site patterns into an array.
mapfile -t SITES < <(grep -vE '^\s*(#|$)' "$SITES_FILE")
N="${#SITES[@]}"
echo "Sampling $N sites -> $OUTDIR/  (max ${MAX_PER_SITE}/site, years ${LO}-${HI})"

i=0
for site in "${SITES[@]}"; do
    i=$((i + 1))
    # Turn 'www.nytimes.com/*' into a safe filename like 'www.nytimes.com.jsonl'.
    safe="$(echo "$site" | sed 's#/\*$##; s#[/*]#_#g')"
    out="$OUTDIR/${safe}.jsonl"

    if [[ -s "$out" ]]; then
        echo "[$i/$N] $site  -> already have $out, skipping"
        continue
    fi

    echo "[$i/$N] $site  -> $out"
    python3 "$SCRIPT" fetch \
        --sites "$site" \
        --per-dump "$PER_DUMP" \
        --max-per-site "$MAX_PER_SITE" \
        --lo "$LO" --hi "$HI" \
        --out "$out" \
        || echo "  !! sampler failed for $site (continuing)"
done

echo "All done. Per-site JSONL in $OUTDIR/"
echo "Combine with:  cat $OUTDIR/*.jsonl > news_corpus.jsonl"
