#!/usr/bin/env python3
"""
analyze_feed.py — Engagement analysis for any Bluesky feed.

Given a Bluesky feed URL (e.g. https://bsky.app/profile/realnsfw.social/feed/aaabgjszdhpyk),
this script paginates the Bluesky public API, filters to posts older than 24 hours,
classifies posts by image count, and runs a full statistical battery:

  - Descriptive stats (mean, median, bootstrap 95% CI) per group
  - Mann-Whitney U for 1 vs 2+
  - Kruskal-Wallis across 1/2/3/4 image groups
  - Pairwise Mann-Whitney with Holm correction
  - Spearman correlation (image count vs metric)
  - Author-level paired Wilcoxon (confound check)
  - Time-of-day stratified analysis (Stouffer combined z)
  - Author-tier split analysis (small/medium/large)
  - Text-feature analysis (caption length, alt-text)

Output: CSV + JSON + PNG charts + interactive HTML dashboard.

Usage:
    python analyze_feed.py FEED_URL [options]

Example:
    python analyze_feed.py "https://bsky.app/profile/realnsfw.social/feed/aaabgjszdhpyk" \
        --target 20000 --output ./my-analysis

Requirements: pip install scipy numpy matplotlib

Uses only Bluesky's public, unauthenticated API (public.api.bsky.app).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import pickle
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from itertools import combinations
from pathlib import Path

import numpy as np
from scipy import stats
from scipy.stats import norm

PUBLIC_API = "https://public.api.bsky.app"


def parse_feed_url(s: str) -> tuple[str, str]:
    """Accepts https://bsky.app/profile/HANDLE/feed/RKEY, at:// URI, or HANDLE/RKEY."""
    s = s.strip()
    if s.startswith("at://"):
        m = re.match(r"at://([^/]+)/app\.bsky\.feed\.generator/([^/?#]+)", s)
        if not m:
            raise ValueError(f"bad at:// URI: {s}")
        return m.group(1), m.group(2)
    if "bsky.app" in s:
        m = re.search(r"profile/([^/]+)/feed/([^/?#]+)", s)
        if not m:
            raise ValueError(f"couldn't parse bsky.app URL: {s}")
        return m.group(1), m.group(2)
    if "/" in s and not s.startswith("http"):
        handle, rkey = s.split("/", 1)
        return handle, rkey
    raise ValueError(f"unrecognized feed specifier: {s}")


def resolve_handle(handle: str) -> str:
    if handle.startswith("did:"):
        return handle
    url = f"{PUBLIC_API}/xrpc/com.atproto.identity.resolveHandle?handle={urllib.parse.quote(handle)}"
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())["did"]


def get_feed(feed_uri: str, cursor: str | None = None, limit: int = 100, retries: int = 4):
    params = {"feed": feed_uri, "limit": str(limit)}
    if cursor:
        params["cursor"] = cursor
    url = f"{PUBLIC_API}/xrpc/app.bsky.feed.getFeed?" + urllib.parse.urlencode(params)
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "bsky-feed-engagement/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(1.2 * (attempt + 1))


def analyze_embed(record: dict) -> tuple[int, str, list[str]]:
    emb = record.get("embed") if record else None
    alts: list[str] = []
    if not emb:
        return 0, "none", alts
    t = emb.get("$type", "")
    if t == "app.bsky.embed.images":
        imgs = emb.get("images") or []
        alts = [(i.get("alt") or "") for i in imgs]
        return len(imgs), "images", alts
    if t == "app.bsky.embed.recordWithMedia":
        media = emb.get("media") or {}
        if media.get("$type") == "app.bsky.embed.images":
            imgs = media.get("images") or []
            alts = [(i.get("alt") or "") for i in imgs]
            return len(imgs), "recordWithMedia.images", alts
        return 0, "recordWithMedia.other", alts
    return 0, t or "unknown", alts


def parse_ts(s: str):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def collect(feed_uri, target, hard_cap, state_path, cutoff, polite_delay=0.04):
    if state_path.exists():
        with open(state_path, "rb") as f:
            st = pickle.load(f)
        rows, seen, cursor = st["rows"], st["seen"], st["cursor"]
        total_fetched, pages, eligible = st["total_fetched"], st["pages"], st["eligible"]
        print(f"[resume] pages={pages} fetched={total_fetched} eligible={eligible}", file=sys.stderr)
    else:
        rows, seen, cursor, total_fetched, pages, eligible = [], set(), None, 0, 0, 0

    last_print = time.time()
    while eligible < target and total_fetched < hard_cap:
        try:
            data = get_feed(feed_uri, cursor)
        except Exception as e:
            print(f"[error] {e}; stopping", file=sys.stderr); break
        items = data.get("feed", [])
        if not items:
            print("[done] no more items", file=sys.stderr); break
        pages += 1
        for item in items:
            post = item.get("post") or {}
            uri = post.get("uri")
            if not uri or uri in seen:
                continue
            seen.add(uri); total_fetched += 1
            rec = post.get("record") or {}
            ts = parse_ts(post.get("indexedAt", "") or "") or parse_ts(rec.get("createdAt", "") or "")
            if ts is None or ts >= cutoff:
                continue
            n, kind, alts = analyze_embed(rec)
            text = rec.get("text", "") or ""
            author = post.get("author") or {}
            rows.append({
                "uri": uri,
                "author_handle": author.get("handle", ""),
                "author_did": author.get("did", ""),
                "indexedAt": post.get("indexedAt", ""),
                "createdAt": rec.get("createdAt", ""),
                "embed_kind": kind,
                "image_count": n,
                "likeCount": post.get("likeCount", 0) or 0,
                "repostCount": post.get("repostCount", 0) or 0,
                "replyCount": post.get("replyCount", 0) or 0,
                "quoteCount": post.get("quoteCount", 0) or 0,
                "text_len": len(text),
                "alt_count_nonempty": sum(1 for a in alts if a.strip()),
                "alt_total_len": sum(len(a) for a in alts),
            })
            if n >= 1:
                eligible += 1
        nc = data.get("cursor")
        if not nc or nc == cursor:
            print("[done] end of feed", file=sys.stderr); break
        cursor = nc
        if pages % 10 == 0:
            with open(state_path, "wb") as f:
                pickle.dump({"rows": rows, "seen": seen, "cursor": cursor,
                             "total_fetched": total_fetched, "pages": pages, "eligible": eligible}, f)
        if time.time() - last_print > 3:
            print(f"[progress] pages={pages} fetched={total_fetched} eligible={eligible}/{target}", file=sys.stderr)
            last_print = time.time()
        time.sleep(polite_delay)

    with open(state_path, "wb") as f:
        pickle.dump({"rows": rows, "seen": seen, "cursor": cursor,
                     "total_fetched": total_fetched, "pages": pages, "eligible": eligible}, f)
    print(f"[final] pages={pages} fetched={total_fetched} eligible={eligible}", file=sys.stderr)
    return [r for r in rows if r["image_count"] >= 1]


def cliffs_delta(x, y):
    U = stats.mannwhitneyu(x, y, alternative="two-sided").statistic
    return 2 * U / (len(x) * len(y)) - 1


def bootstrap_ci(arr, fn=np.mean, n_boot=5000, seed=42):
    arr = np.asarray(arr)
    if len(arr) == 0:
        return (None, None)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(arr), size=(n_boot, len(arr)))
    vals = fn(arr[idx], axis=1)
    return float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5))


def holm(ps):
    order = sorted(range(len(ps)), key=lambda i: ps[i])
    adj = [1.0] * len(ps); running = 0.0
    for rank, i in enumerate(order):
        v = min(1.0, ps[i] * (len(ps) - rank))
        running = max(running, v); adj[i] = running
    return adj


def run_stats(img_rows: list[dict]) -> dict:
    for r in img_rows:
        r["total_engagement"] = r["likeCount"] + r["repostCount"] + r["replyCount"] + r["quoteCount"]
        ts = datetime.fromisoformat(r["indexedAt"].replace("Z", "+00:00"))
        r["hour"] = ts.hour
        r["dow"] = ts.weekday()

    single = [r for r in img_rows if r["image_count"] == 1]
    multi = [r for r in img_rows if r["image_count"] >= 2]
    metrics = ["likeCount", "repostCount", "replyCount", "quoteCount", "total_engagement"]

    main = {
        "n_total": len(img_rows), "n_single": len(single), "n_multi": len(multi),
        "unique_authors": len(set(r["author_handle"] for r in img_rows)),
        "date_range": [min(r["indexedAt"] for r in img_rows), max(r["indexedAt"] for r in img_rows)],
        "metrics": {},
    }
    for m in metrics:
        xs = np.array([r[m] for r in single]); ys = np.array([r[m] for r in multi])
        mw = stats.mannwhitneyu(xs, ys, alternative="two-sided")
        main["metrics"][m] = {
            "single": {"n": len(xs), "mean": float(xs.mean()), "median": float(np.median(xs))},
            "multi": {"n": len(ys), "mean": float(ys.mean()), "median": float(np.median(ys))},
            "mw_U": float(mw.statistic), "mw_p": float(mw.pvalue),
            "cd": float(cliffs_delta(xs, ys)),
            "ci_mean_s": bootstrap_ci(xs), "ci_mean_m": bootstrap_ci(ys),
        }

    groups = {ic: [r for r in img_rows if r["image_count"] == ic] for ic in (1, 2, 3, 4)}
    per_count = {}
    for m in metrics:
        samples = [np.array([r[m] for r in groups[ic]]) for ic in (1, 2, 3, 4)]
        kw = stats.kruskal(*samples)
        x_img = np.array([r["image_count"] for r in img_rows])
        y_met = np.array([r[m] for r in img_rows])
        sp = stats.spearmanr(x_img, y_met)
        d = {"by_count": {}, "pairs": [],
             "kruskal_H": float(kw.statistic), "kruskal_p": float(kw.pvalue),
             "spearman_r": float(sp.statistic), "spearman_p": float(sp.pvalue)}
        for ic, arr in zip((1, 2, 3, 4), samples):
            if len(arr) > 0:
                d["by_count"][ic] = {"n": len(arr), "mean": float(arr.mean()),
                                     "median": float(np.median(arr)),
                                     "ci_mean": bootstrap_ci(arr)}
            else:
                d["by_count"][ic] = {"n": 0, "mean": None, "median": None, "ci_mean": (None, None)}
        raw_ps = []
        for (a, b) in combinations((1, 2, 3, 4), 2):
            if len(samples[a-1]) == 0 or len(samples[b-1]) == 0:
                continue
            mw = stats.mannwhitneyu(samples[a-1], samples[b-1], alternative="two-sided")
            U, p = float(mw.statistic), float(mw.pvalue)
            cd = 2 * U / (len(samples[a-1]) * len(samples[b-1])) - 1
            raw_ps.append([a, b, U, p, cd])
        if raw_ps:
            adj = holm([r[3] for r in raw_ps])
            d["pairs"] = [[a, b, U, p, adj[i], cd] for i, (a, b, U, p, cd) in enumerate(raw_ps)]
        per_count[m] = d

    authors = defaultdict(lambda: {"single": [], "multi": []})
    for r in img_rows:
        key = "single" if r["image_count"] == 1 else "multi"
        authors[r["author_handle"]][key].append(r)
    paired = [(a, v["single"], v["multi"]) for a, v in authors.items()
              if len(v["single"]) >= 2 and len(v["multi"]) >= 2]
    confound = {"n_authors": len(authors), "n_paired": len(paired),
                "n_only_single": sum(1 for v in authors.values() if v["single"] and not v["multi"]),
                "n_only_multi": sum(1 for v in authors.values() if v["multi"] and not v["single"]),
                "n_both": sum(1 for v in authors.values() if v["single"] and v["multi"])}
    for m in metrics:
        diffs = np.array([np.mean([r[m] for r in s_]) - np.mean([r[m] for r in mu_])
                          for _, s_, mu_ in paired])
        wp = float(stats.wilcoxon(diffs, alternative="two-sided").pvalue) if len(diffs) >= 10 and np.any(diffs != 0) else None
        confound[m] = {"mean_diff": float(diffs.mean()) if len(diffs) else None, "wilcoxon_p": wp}

    hour_mean = {ic: [None]*24 for ic in (1, 2, 3, 4)}
    for ic in (1, 2, 3, 4):
        for h in range(24):
            arr = [r["likeCount"] for r in groups[ic] if r["hour"] == h]
            hour_mean[ic][h] = float(np.mean(arr)) if arr else None
    zs, ws = [], []
    for h in range(24):
        a = [r["likeCount"] for r in groups[1] if r["hour"] == h]
        b = [r["likeCount"] for r in groups[4] if r["hour"] == h]
        if len(a) >= 10 and len(b) >= 5:
            mw = stats.mannwhitneyu(a, b, alternative="two-sided")
            n1, n2 = len(a), len(b)
            mu = n1 * n2 / 2; sd = (n1 * n2 * (n1 + n2 + 1) / 12) ** 0.5
            zs.append((mw.statistic - mu) / sd); ws.append(n1 + n2)
    stouffer = None
    if zs:
        zs_a, ws_a = np.array(zs), np.array(ws)
        combined_z = (ws_a * zs_a).sum() / np.sqrt((ws_a ** 2).sum())
        stouffer = {"z": float(combined_z), "p": float(2 * (1 - norm.cdf(abs(combined_z)))), "n_strata": len(zs)}

    text = {"by_count": {}}
    for ic in (1, 2, 3, 4):
        rs = groups[ic]
        if not rs: continue
        text["by_count"][ic] = {
            "n": len(rs),
            "text_len_mean": float(np.mean([r["text_len"] for r in rs])),
            "text_len_median": float(np.median([r["text_len"] for r in rs])),
            "alt_any_rate": float(np.mean([1 if r["alt_count_nonempty"] > 0 else 0 for r in rs])),
        }
    buckets = [(0, 1), (1, 50), (50, 150), (150, 300), (300, 1000)]
    strat = []
    for lo, hi in buckets:
        a = [r["likeCount"] for r in groups[1] if lo <= r["text_len"] < hi]
        b = [r["likeCount"] for r in groups[4] if lo <= r["text_len"] < hi]
        if len(a) >= 10 and len(b) >= 5:
            mw = stats.mannwhitneyu(a, b, alternative="two-sided")
            strat.append({"bucket": f"{lo}-{hi}", "n_1": len(a), "n_4": len(b),
                          "mean_1": float(np.mean(a)), "mean_4": float(np.mean(b)),
                          "mw_p": float(mw.pvalue), "cd": float(cliffs_delta(a, b))})
    text["caption_stratified_1_vs_4"] = strat

    return {"main": main, "per_count": per_count, "confound": confound,
            "time": {"hour_mean_by_ic": hour_mean, "stouffer_1_vs_4": stouffer},
            "text": text, "generated_at": datetime.now(timezone.utc).isoformat()}


def write_csv(rows, path):
    if not rows: return
    fields = list(rows[0].keys())
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def render_charts(stats_data, out_dir):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    out_dir.mkdir(parents=True, exist_ok=True)
    pc = ["#3b82f6", "#14b8a6", "#a855f7", "#ef4444"]
    p = stats_data["per_count"]
    def bc(d, ic): return d.get(ic) or d.get(str(ic)) or {}

    # mean likes
    ics = [1, 2, 3, 4]
    means = [bc(p["likeCount"]["by_count"], ic).get("mean", 0) or 0 for ic in ics]
    ci_lo = [(bc(p["likeCount"]["by_count"], ic).get("ci_mean") or [0, 0])[0] for ic in ics]
    ci_hi = [(bc(p["likeCount"]["by_count"], ic).get("ci_mean") or [0, 0])[1] for ic in ics]
    ns = [bc(p["likeCount"]["by_count"], ic).get("n", 0) for ic in ics]
    fig, ax = plt.subplots(figsize=(7, 4), dpi=120)
    ax.bar(range(4), means, color=pc,
           yerr=[[m-l for m,l in zip(means,ci_lo)], [h-m for m,h in zip(means,ci_hi)]],
           capsize=6)
    ax.set_xticks(range(4)); ax.set_xticklabels([f"{ic} img\n(n={ns[i]:,})" for i, ic in enumerate(ics)])
    ax.set_ylabel("mean likes"); ax.set_title("Mean likes by image count (95% CI)")
    fig.tight_layout(); fig.savefig(out_dir / "per_count_likes.png", bbox_inches="tight"); plt.close(fig)

    print(f"[charts] wrote charts to {out_dir}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description="Analyze engagement by image count for any Bluesky feed.")
    ap.add_argument("feed", help="Feed URL: https://bsky.app/profile/HANDLE/feed/RKEY, at:// URI, or HANDLE/RKEY")
    ap.add_argument("--target", type=int, default=10000, help="Target number of eligible posts (default 10000)")
    ap.add_argument("--hard-cap", type=int, default=60000, help="Max total posts fetched (default 60000)")
    ap.add_argument("--hours", type=float, default=24, help="Exclude posts newer than N hours (default 24)")
    ap.add_argument("--cutoff", default=None, help="Alternative: fixed ISO cutoff (e.g. 2026-04-23T00:00:00Z)")
    ap.add_argument("--output", default="./output", help="Output directory (default ./output)")
    ap.add_argument("--no-charts", action="store_true")
    args = ap.parse_args()

    handle_or_did, rkey = parse_feed_url(args.feed)
    did = resolve_handle(handle_or_did)
    feed_uri = f"at://{did}/app.bsky.feed.generator/{rkey}"
    print(f"[feed] {feed_uri}", file=sys.stderr)

    out = Path(args.output); out.mkdir(parents=True, exist_ok=True)
    state_path = out / ".state.pkl"
    if args.cutoff:
        cutoff = datetime.fromisoformat(args.cutoff.replace("Z", "+00:00"))
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    print(f"[cutoff] excluding posts at or after {cutoff.isoformat()}", file=sys.stderr)

    img_rows = collect(feed_uri, args.target, args.hard_cap, state_path, cutoff)
    if not img_rows:
        print("[error] no posts collected", file=sys.stderr); sys.exit(1)

    stats_data = run_stats(img_rows)
    write_csv(img_rows, out / "feed_engagement.csv")
    (out / "all_stats.json").write_text(json.dumps(stats_data, indent=2, default=str))
    if not args.no_charts:
        render_charts(stats_data, out / "charts")

    m = stats_data["main"]; p = stats_data["per_count"]
    print("\n" + "=" * 60)
    print(f"FEED: {handle_or_did}/{rkey}")
    print("=" * 60)
    print(f"Sample: {m['n_total']:,} image posts, {m['unique_authors']} authors")
    print("Mean likes by image count:")
    for ic in (1, 2, 3, 4):
        d = p["likeCount"]["by_count"].get(ic, {})
        if d.get("n"):
            print(f"  {ic} img: n={d['n']:>5}  mean={d['mean']:>7.2f}  median={d['median']:>5}")
    print(f"Kruskal-Wallis p={p['likeCount']['kruskal_p']:.3g}")
    print(f"1 vs 2+: MW p={m['metrics']['likeCount']['mw_p']:.3g}  d={m['metrics']['likeCount']['cd']:+.3f}")


if __name__ == "__main__":
    main()
