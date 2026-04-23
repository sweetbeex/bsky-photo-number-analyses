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
    python analyze_feed.py "https://bsky.app/profile/realnsfw.social/feed/aaabgjszdhpyk" \\
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

# ---------------------------------------------------------------------------
# Feed URL parsing
# ---------------------------------------------------------------------------

def parse_feed_url(s: str) -> tuple[str, str]:
    """
    Accepts either:
      - https://bsky.app/profile/HANDLE/feed/RKEY
      - at://DID/app.bsky.feed.generator/RKEY
      - HANDLE/RKEY
    Returns (handle_or_did, rkey).
    """
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

# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

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
    """Return (image_count, embed_kind, alt_texts)."""
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

def collect(feed_uri: str, target: int, hard_cap: int, state_path: Path,
            cutoff: datetime, polite_delay: float = 0.04) -> list[dict]:
    if state_path.exists():
        with open(state_path, "rb") as f:
            st = pickle.load(f)
        rows = st["rows"]; seen = st["seen"]; cursor = st["cursor"]
        total_fetched = st["total_fetched"]; pages = st["pages"]; eligible = st["eligible"]
        print(f"[resume] pages={pages} fetched={total_fetched} eligible={eligible}", file=sys.stderr)
    else:
        rows, seen, cursor, total_fetched, pages, eligible = [], set(), None, 0, 0, 0

    last_print = time.time()
    while eligible < target and total_fetched < hard_cap:
        try:
            data = get_feed(feed_uri, cursor)
        except Exception as e:
            print(f"[error] {e}; saving state and stopping", file=sys.stderr); break
        items = data.get("feed", [])
        if not items:
            print("[done] no more items", file=sys.stderr); cursor = None; break
        pages += 1
        for item in items:
            post = item.get("post") or {}
            uri = post.get("uri")
            if not uri or uri in seen:
                continue
            seen.add(uri)
            total_fetched += 1
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
            print("[done] end of feed", file=sys.stderr); cursor = None; break
        cursor = nc

        # save state every 10 pages for resumability
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

# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

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
    m_tests = len(ps)
    adj = [1.0] * len(ps); running = 0.0
    for rank, i in enumerate(order):
        v = min(1.0, ps[i] * (m_tests - rank))
        running = max(running, v); adj[i] = running
    return adj

def run_stats(img_rows: list[dict]) -> dict:
    for r in img_rows:
        r["total_engagement"] = r["likeCount"] + r["repostCount"] + r["replyCount"] + r["quoteCount"]
        ts = datetime.fromisoformat(r["indexedAt"].replace("Z", "+00:00"))
        r["hour"] = ts.hour
        r["dow"] = ts.weekday()

    single = [r for r in img_rows if r["image_count"] == 1]
    multi  = [r for r in img_rows if r["image_count"] >= 2]
    metrics = ["likeCount", "repostCount", "replyCount", "quoteCount", "total_engagement"]

    # main: 1 vs 2+
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
            "multi":  {"n": len(ys), "mean": float(ys.mean()), "median": float(np.median(ys))},
            "mw_U": float(mw.statistic), "mw_p": float(mw.pvalue),
            "cd": float(cliffs_delta(xs, ys)),
            "ci_mean_s": bootstrap_ci(xs), "ci_mean_m": bootstrap_ci(ys),
        }

    # per-count
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
        # pairwise Mann-Whitney with Holm
        pairs = list(combinations((1, 2, 3, 4), 2))
        raw_ps = []
        for (a, b) in pairs:
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

    # author confound
    authors = defaultdict(lambda: {"single": [], "multi": []})
    for r in img_rows:
        key = "single" if r["image_count"] == 1 else "multi"
        authors[r["author_handle"]][key].append(r)
    paired = [(a, v["single"], v["multi"]) for a, v in authors.items()
              if len(v["single"]) >= 2 and len(v["multi"]) >= 2]
    confound = {
        "n_authors": len(authors),
        "n_only_single": sum(1 for v in authors.values() if v["single"] and not v["multi"]),
        "n_only_multi": sum(1 for v in authors.values() if v["multi"] and not v["single"]),
        "n_both": sum(1 for v in authors.values() if v["single"] and v["multi"]),
        "n_paired": len(paired),
    }
    for m in metrics:
        diffs = []
        for _, s_, mu_ in paired:
            diffs.append(np.mean([r[m] for r in s_]) - np.mean([r[m] for r in mu_]))
        diffs = np.array(diffs)
        wp = float(stats.wilcoxon(diffs, alternative="two-sided").pvalue) if len(diffs) >= 10 and np.any(diffs != 0) else None
        confound[m] = {"mean_diff": float(diffs.mean()) if len(diffs) else None,
                       "median_diff": float(np.median(diffs)) if len(diffs) else None,
                       "wilcoxon_p": wp}

    # time of day
    hour_mean = {ic: [None]*24 for ic in (1, 2, 3, 4)}
    for ic in (1, 2, 3, 4):
        for h in range(24):
            arr = [r["likeCount"] for r in groups[ic] if r["hour"] == h]
            hour_mean[ic][h] = float(np.mean(arr)) if arr else None
    dow_mean = {ic: [None]*7 for ic in (1, 2, 3, 4)}
    for ic in (1, 2, 3, 4):
        for d in range(7):
            arr = [r["likeCount"] for r in groups[ic] if r["dow"] == d]
            dow_mean[ic][d] = float(np.mean(arr)) if arr else None
    # hour-stratified 1 vs 4 (Stouffer combined z)
    zs, ws = [], []
    for h in range(24):
        a = [r["likeCount"] for r in groups[1] if r["hour"] == h]
        b = [r["likeCount"] for r in groups[4] if r["hour"] == h]
        if len(a) >= 10 and len(b) >= 5:
            mw = stats.mannwhitneyu(a, b, alternative="two-sided")
            n1, n2 = len(a), len(b)
            mu = n1 * n2 / 2; sd = (n1 * n2 * (n1 + n2 + 1) / 12) ** 0.5
            z = (mw.statistic - mu) / sd
            zs.append(z); ws.append(n1 + n2)
    stouffer = None
    if zs:
        zs_a, ws_a = np.array(zs), np.array(ws)
        combined_z = (ws_a * zs_a).sum() / np.sqrt((ws_a ** 2).sum())
        stouffer = {"z": float(combined_z), "p": float(2 * (1 - norm.cdf(abs(combined_z)))), "n_strata": len(zs)}
    time_an = {"hour_mean_by_ic": hour_mean, "dow_mean_by_ic": dow_mean, "stouffer_1_vs_4": stouffer}

    # author tier
    author_median = {a: float(np.median([r["likeCount"] for r in v["single"] + v["multi"]]))
                     for a, v in authors.items()}
    author_n = {a: len(v["single"]) + len(v["multi"]) for a, v in authors.items()}
    qualified = {a: m for a, m in author_median.items() if author_n[a] >= 5}
    tier = {}
    if len(qualified) >= 6:
        vals = list(qualified.values())
        t1 = float(np.percentile(vals, 33.33)); t2 = float(np.percentile(vals, 66.67))
        tier_map = {a: ("small" if m <= t1 else "medium" if m <= t2 else "large") for a, m in qualified.items()}
        for t_name in ("small", "medium", "large"):
            t_authors = {a for a, t in tier_map.items() if t == t_name}
            t_rows = [r for r in img_rows if r["author_handle"] in t_authors]
            by_ic = {ic: [r["likeCount"] for r in t_rows if r["image_count"] == ic] for ic in (1, 2, 3, 4)}
            tier[t_name] = {"n_authors": len(t_authors), "n_posts": len(t_rows), "by_count": {}}
            for ic in (1, 2, 3, 4):
                tier[t_name]["by_count"][ic] = {"n": len(by_ic[ic]),
                                                "mean": float(np.mean(by_ic[ic])) if by_ic[ic] else None,
                                                "median": float(np.median(by_ic[ic])) if by_ic[ic] else None}
            if len(by_ic[1]) >= 10 and len(by_ic[4]) >= 5:
                mw = stats.mannwhitneyu(by_ic[1], by_ic[4], alternative="two-sided")
                tier[t_name]["mw_1_vs_4"] = {"U": float(mw.statistic), "p": float(mw.pvalue),
                                             "cd": float(cliffs_delta(by_ic[1], by_ic[4]))}
        tier["_thresholds"] = {"small_max": t1, "medium_max": t2}

    # text features
    text = {"by_count": {}}
    for ic in (1, 2, 3, 4):
        rs = groups[ic]
        if not rs: continue
        text["by_count"][ic] = {
            "n": len(rs),
            "text_len_mean": float(np.mean([r["text_len"] for r in rs])),
            "text_len_median": float(np.median([r["text_len"] for r in rs])),
            "alt_any_rate": float(np.mean([1 if r["alt_count_nonempty"] > 0 else 0 for r in rs])),
            "alt_total_len_mean": float(np.mean([r["alt_total_len"] for r in rs])),
        }
    # correlations
    corrs = {}
    for feat in ("text_len", "alt_count_nonempty", "alt_total_len"):
        fv = np.array([r[feat] for r in img_rows])
        for m in metrics:
            mv = np.array([r[m] for r in img_rows])
            sp = stats.spearmanr(fv, mv)
            corrs[f"{feat}__{m}"] = {"r": float(sp.statistic), "p": float(sp.pvalue)}
    text["corrs"] = corrs
    # caption-length stratified 1 vs 4
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
            "time": time_an, "tier": tier, "text": text,
            "generated_at": datetime.now(timezone.utc).isoformat()}

# ---------------------------------------------------------------------------
# Output files
# ---------------------------------------------------------------------------

def write_csv(rows: list[dict], path: Path):
    if not rows:
        return
    fields = list(rows[0].keys())
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

def render_charts(stats_data: dict, out_dir: Path):
    """Render PNG charts that can be embedded in a GitHub README."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 10,
        "axes.edgecolor": "#444", "axes.labelcolor": "#222",
        "axes.titlesize": 11, "axes.titleweight": "bold",
        "figure.facecolor": "white", "axes.facecolor": "white",
    })
    pc_colors = ["#3b82f6", "#14b8a6", "#a855f7", "#ef4444"]
    out_dir.mkdir(parents=True, exist_ok=True)

    p = stats_data["per_count"]
    t = stats_data["time"]
    tier = stats_data["tier"]
    text = stats_data["text"]

    # robust key lookup (stats_data from JSON has string keys; from Python has int)
    def _bc(d, ic):
        return d.get(ic) or d.get(str(ic)) or {}

    # 1. Per-image-count mean likes with CI
    fig, ax = plt.subplots(figsize=(7, 4), dpi=120)
    ics = [1, 2, 3, 4]
    means = [_bc(p["likeCount"]["by_count"], ic).get("mean", 0) or 0 for ic in ics]
    ci_lo = [(_bc(p["likeCount"]["by_count"], ic).get("ci_mean") or [0,0])[0] for ic in ics]
    ci_hi = [(_bc(p["likeCount"]["by_count"], ic).get("ci_mean") or [0,0])[1] for ic in ics]
    ns    = [_bc(p["likeCount"]["by_count"], ic).get("n", 0) for ic in ics]
    err_lo = [m - lo for m, lo in zip(means, ci_lo)]
    err_hi = [hi - m for m, hi in zip(means, ci_hi)]
    ax.bar(range(4), means, color=pc_colors, yerr=[err_lo, err_hi], capsize=6, edgecolor="#222", linewidth=0.7)
    ax.set_xticks(range(4)); ax.set_xticklabels([f"{ic} img\n(n={ns[i]:,})" for i, ic in enumerate(ics)])
    ax.set_ylabel("mean likes"); ax.set_title("Mean likes by image count (95% bootstrap CI)")
    ax.grid(axis="y", linestyle=":", alpha=0.4)
    for i, m in enumerate(means):
        ax.text(i, m + (max(means) * 0.02), f"{m:.1f}", ha="center", fontsize=9, fontweight="bold")
    fig.tight_layout()
    fig.savefig(out_dir / "per_count_likes.png", bbox_inches="tight")
    plt.close(fig)

    # 2. Median likes per image count
    fig, ax = plt.subplots(figsize=(7, 4), dpi=120)
    meds = [_bc(p["likeCount"]["by_count"], ic).get("median", 0) or 0 for ic in ics]
    ax.bar(range(4), meds, color=pc_colors, edgecolor="#222", linewidth=0.7)
    ax.set_xticks(range(4)); ax.set_xticklabels([f"{ic} img\n(n={ns[i]:,})" for i, ic in enumerate(ics)])
    ax.set_ylabel("median likes"); ax.set_title("Median likes by image count")
    ax.grid(axis="y", linestyle=":", alpha=0.4)
    for i, m in enumerate(meds):
        ax.text(i, m + (max(meds) * 0.02), f"{m:.0f}", ha="center", fontsize=9, fontweight="bold")
    fig.tight_layout()
    fig.savefig(out_dir / "per_count_median_likes.png", bbox_inches="tight")
    plt.close(fig)

    # 3. Hour-of-day by image count
    fig, ax = plt.subplots(figsize=(8, 4), dpi=120)
    for i, ic in enumerate(ics):
        arr = t["hour_mean_by_ic"].get(ic) or t["hour_mean_by_ic"].get(str(ic))
        if arr is None: continue
        ax.plot(range(24), arr, marker="o", color=pc_colors[i], label=f"{ic} img", linewidth=1.5, markersize=3.5)
    ax.set_xlabel("hour (UTC)"); ax.set_ylabel("mean likes")
    ax.set_title("Mean likes by posting hour of day")
    ax.set_xticks(range(0, 24, 2))
    ax.legend(loc="upper right", fontsize=9, frameon=True)
    ax.grid(True, linestyle=":", alpha=0.4)
    fig.tight_layout()
    fig.savefig(out_dir / "hour_of_day.png", bbox_inches="tight")
    plt.close(fig)

    # 4. Author tier comparison
    if tier and "small" in tier:
        fig, ax = plt.subplots(figsize=(7, 4), dpi=120)
        tiers = ["small", "medium", "large"]
        x = np.arange(len(tiers)); width = 0.2
        for i, ic in enumerate(ics):
            vals = []
            for tn in tiers:
                by = _bc(tier[tn]["by_count"], ic)
                vals.append((by.get("mean") if by else 0) or 0)
            ax.bar(x + i * width, vals, width, label=f"{ic} img", color=pc_colors[i], edgecolor="#222", linewidth=0.5)
        ax.set_xticks(x + width * 1.5)
        ax.set_xticklabels([f"{tn}\n(n={tier[tn]['n_authors']} authors)" for tn in tiers])
        ax.set_ylabel("mean likes"); ax.set_title("Mean likes by image count × author tier")
        ax.legend(loc="upper left", fontsize=9)
        ax.grid(axis="y", linestyle=":", alpha=0.4)
        fig.tight_layout()
        fig.savefig(out_dir / "author_tier.png", bbox_inches="tight")
        plt.close(fig)

    # 5. Caption length stratified 1 vs 4
    strat = text["caption_stratified_1_vs_4"]
    if strat:
        fig, ax = plt.subplots(figsize=(8, 4), dpi=120)
        labels = [s["bucket"] for s in strat]
        x = np.arange(len(labels)); width = 0.38
        m1 = [s["mean_1"] for s in strat]; m4 = [s["mean_4"] for s in strat]
        bars1 = ax.bar(x - width/2, m1, width, label="1 img", color=pc_colors[0], edgecolor="#222", linewidth=0.5)
        bars4 = ax.bar(x + width/2, m4, width, label="4 img", color=pc_colors[3], edgecolor="#222", linewidth=0.5)
        for i, s in enumerate(strat):
            sig = "***" if s["mw_p"] < 0.001 else "**" if s["mw_p"] < 0.01 else "*" if s["mw_p"] < 0.05 else "ns"
            y_top = max(m1[i], m4[i])
            ax.text(x[i], y_top + max(m1+m4)*0.03, sig, ha="center", fontsize=9, fontweight="bold")
        ax.set_xticks(x); ax.set_xticklabels([f"{l}\nchars" for l in labels])
        ax.set_ylabel("mean likes"); ax.set_title("1-img vs 4-img likes, stratified by caption length")
        ax.legend(); ax.grid(axis="y", linestyle=":", alpha=0.4)
        fig.tight_layout()
        fig.savefig(out_dir / "caption_stratified.png", bbox_inches="tight")
        plt.close(fig)

    # 6. Caption length and alt-text rate by image count
    fig, axes = plt.subplots(1, 2, figsize=(10, 4), dpi=120)
    tb = text["by_count"]
    cap_means = [(tb.get(ic) or tb.get(str(ic)) or {}).get("text_len_mean", 0) for ic in ics]
    alt_rates = [(tb.get(ic) or tb.get(str(ic)) or {}).get("alt_any_rate", 0) * 100 for ic in ics]
    axes[0].bar(range(4), cap_means, color=pc_colors, edgecolor="#222", linewidth=0.5)
    axes[0].set_xticks(range(4)); axes[0].set_xticklabels([f"{ic} img" for ic in ics])
    axes[0].set_ylabel("mean caption length (chars)"); axes[0].set_title("Caption length by image count")
    axes[0].grid(axis="y", linestyle=":", alpha=0.4)
    for i, v in enumerate(cap_means):
        axes[0].text(i, v + max(cap_means)*0.02, f"{v:.0f}", ha="center", fontsize=9, fontweight="bold")
    axes[1].bar(range(4), alt_rates, color=pc_colors, edgecolor="#222", linewidth=0.5)
    axes[1].set_xticks(range(4)); axes[1].set_xticklabels([f"{ic} img" for ic in ics])
    axes[1].set_ylabel("% of posts with alt text"); axes[1].set_title("Alt-text usage rate by image count")
    axes[1].grid(axis="y", linestyle=":", alpha=0.4)
    for i, v in enumerate(alt_rates):
        axes[1].text(i, v + max(alt_rates)*0.02, f"{v:.1f}%", ha="center", fontsize=9, fontweight="bold")
    fig.tight_layout()
    fig.savefig(out_dir / "caption_and_alt.png", bbox_inches="tight")
    plt.close(fig)

    # 7. 1 vs 2+ means
    m = stats_data["main"]["metrics"]
    fig, ax = plt.subplots(figsize=(7, 4), dpi=120)
    labels = ["likes", "reposts", "replies", "quotes", "total"]
    keys = ["likeCount", "repostCount", "replyCount", "quoteCount", "total_engagement"]
    single = [m[k]["single"]["mean"] for k in keys]
    multi  = [m[k]["multi"]["mean"] for k in keys]
    x = np.arange(len(labels)); width = 0.38
    ax.bar(x - width/2, single, width, label="1 photo",  color="#3b82f6", edgecolor="#222", linewidth=0.5)
    ax.bar(x + width/2, multi,  width, label="2+ photos", color="#ec4899", edgecolor="#222", linewidth=0.5)
    ax.set_xticks(x); ax.set_xticklabels(labels)
    ax.set_ylabel("mean"); ax.set_title("1 photo vs 2+ photos — mean engagement")
    ax.legend(); ax.grid(axis="y", linestyle=":", alpha=0.4)
    fig.tight_layout()
    fig.savefig(out_dir / "main_comparison.png", bbox_inches="tight")
    plt.close(fig)

    print(f"[charts] wrote 7 PNG charts to {out_dir}", file=sys.stderr)

# ---------------------------------------------------------------------------
# Dashboard HTML (interactive, Chart.js)
# ---------------------------------------------------------------------------

DASHBOARD_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ \u2014 engagement analysis</title>
<style>
:root { color-scheme: light; } * { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color:#111; background:#fafafa; margin:0; line-height:1.5; }
.container { max-width: 1100px; margin: 0 auto; padding: 24px; background:#fff; }
h1 { font-size:24px; margin:0 0 6px; } h2 { font-size:17px; margin:24px 0 10px; border-bottom:2px solid #111; padding-bottom:6px; } h3 { font-size:13px; margin:0 0 8px; color:#333; }
.subtitle { color:#333; font-size:13px; margin-bottom:20px; padding-bottom:10px; border-bottom:1px solid #e4e4e7; }
.grid { display:grid; grid-template-columns:1fr 1fr; gap:16px; } @media (max-width:820px){ .grid{grid-template-columns:1fr;} }
.card { border:1px solid #e4e4e7; border-radius:8px; padding:14px; }
.chart { position:relative; height:260px; }
table { width:100%; border-collapse:collapse; font-size:12px; }
th,td { padding:6px 8px; text-align:right; border-bottom:1px solid #eee; } th:first-child, td:first-child { text-align:left; } th{background:#f4f4f5;font-weight:600;}
.tldr { background:#eff6ff; border:1px solid #bfdbfe; border-radius:8px; padding:14px; margin:14px 0; font-size:13px; }
.tldr h3 { color:#1e40af; margin:0 0 8px; } .tldr ol { margin:0; padding-left:20px; } .tldr li { margin:6px 0; }
</style></head><body><div class="container">
<h1>__TITLE__</h1><div class="subtitle" id="subtitle"></div>
<h2>Top-line: 1 photo vs 2+ photos</h2>
<div class="grid"><div class="card"><h3>Means</h3><div class="chart"><canvas id="mainChart"></canvas></div></div>
<div class="card"><h3>Summary</h3><div style="overflow:auto"><table id="sumTable"></table></div></div></div>
<h2>Per-image count</h2>
<div class="grid"><div class="card"><h3>Mean likes (95% CI)</h3><div class="chart"><canvas id="pcLikes"></canvas></div></div>
<div class="card"><h3>Pairwise Mann-Whitney (Holm)</h3><div style="overflow:auto"><table id="pwTable"></table></div></div></div>
<h2>Time of day</h2>
<div class="grid"><div class="card"><h3>Mean likes by hour (UTC)</h3><div class="chart"><canvas id="hourChart"></canvas></div></div>
<div class="card"><h3>Stouffer combined (1 vs 4)</h3><div id="stoufferDiv" style="font-size:13px;padding-top:8px"></div></div></div>
<h2>Author tier</h2>
<div class="grid"><div class="card"><h3>Mean likes by image count \u00d7 tier</h3><div class="chart"><canvas id="tierChart"></canvas></div></div>
<div class="card"><h3>Tier-by-tier 1 vs 4</h3><div style="overflow:auto"><table id="tierTable"></table></div></div></div>
<h2>Text features</h2>
<div class="grid"><div class="card"><h3>Caption length by image count</h3><div class="chart"><canvas id="capChart"></canvas></div></div>
<div class="card"><h3>1 vs 4 stratified by caption length</h3><div style="overflow:auto"><table id="capTable"></table></div></div></div>
</div>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.5.0/dist/chart.umd.js" integrity="sha384-iU8HYtnGQ8Cy4zl7gbNMOhsDTTKX02BTXptVP/vqAWIaTfM7isw76iyZCsjL2eVi" crossorigin="anonymous"></script>
<script>
const D = __PAYLOAD__;
const PC=["#3b82f6","#14b8a6","#a855f7","#ef4444"];
const m=D.main, p=D.per_count, t=D.time, tier=D.tier, text=D.text;
function pStar(pv){return pv<0.001?"***":pv<0.01?"**":pv<0.05?"*":"ns";}
document.getElementById("subtitle").innerHTML="n=<b>"+m.n_total.toLocaleString()+"</b> image posts across <b>"+m.unique_authors.toLocaleString()+"</b> unique authors. Range: "+m.date_range[0].slice(0,10)+" \u2192 "+m.date_range[1].slice(0,10)+".";
// main chart
new Chart(document.getElementById("mainChart"),{type:"bar",data:{labels:["likes","reposts","replies","quotes","total"],datasets:[{label:"1 photo",data:["likeCount","repostCount","replyCount","quoteCount","total_engagement"].map(k=>m.metrics[k].single.mean),backgroundColor:"#3b82f6"},{label:"2+ photos",data:["likeCount","repostCount","replyCount","quoteCount","total_engagement"].map(k=>m.metrics[k].multi.mean),backgroundColor:"#ec4899"}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{position:"bottom"}},scales:{y:{beginAtZero:true}}}});
// per-count
(function(){const ics=["1","2","3","4"];const vals=ics.map(i=>p.likeCount.by_count[i].mean);const cis=ics.map(i=>p.likeCount.by_count[i].ci_mean);const ns=ics.map(i=>p.likeCount.by_count[i].n);new Chart(document.getElementById("pcLikes"),{type:"bar",data:{labels:ics.map((l,i)=>l+" img (n="+ns[i]+")"),datasets:[{data:vals,backgroundColor:PC}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false},tooltip:{callbacks:{afterLabel:(ctx)=>"CI ["+cis[ctx.dataIndex][0].toFixed(1)+", "+cis[ctx.dataIndex][1].toFixed(1)+"]"}}},scales:{y:{beginAtZero:true}}},plugins:[{afterDatasetsDraw(ch){const {ctx,scales:{x,y}}=ch;ctx.save();ctx.strokeStyle="#333";ctx.lineWidth=1.5;ch.data.datasets[0].data.forEach((v,i)=>{const [lo,hi]=cis[i];const cx=x.getPixelForValue(i),yL=y.getPixelForValue(lo),yH=y.getPixelForValue(hi);ctx.beginPath();ctx.moveTo(cx,yL);ctx.lineTo(cx,yH);ctx.stroke();ctx.beginPath();ctx.moveTo(cx-6,yL);ctx.lineTo(cx+6,yL);ctx.stroke();ctx.beginPath();ctx.moveTo(cx-6,yH);ctx.lineTo(cx+6,yH);ctx.stroke();});ctx.restore();}}]});
let html='<thead><tr><th>Pair</th><th>p (Holm)</th><th>Cliff\u2019s d</th></tr></thead><tbody>';for(const r of p.likeCount.pairs){const [a,b,U,pr,ph,cd]=r;html+='<tr><td>'+a+' vs '+b+'</td><td>'+ph.toPrecision(3)+' '+pStar(ph)+'</td><td>'+(cd>=0?"+":"")+cd.toFixed(3)+'</td></tr>';}document.getElementById("pwTable").innerHTML=html+'</tbody>';})();
// hour
new Chart(document.getElementById("hourChart"),{type:"line",data:{labels:Array.from({length:24},(_,i)=>i),datasets:[1,2,3,4].map((ic,i)=>({label:ic+" img",data:t.hour_mean_by_ic[ic]||t.hour_mean_by_ic[String(ic)],borderColor:PC[i],backgroundColor:PC[i],pointRadius:2,borderWidth:2,tension:0.3,fill:false,spanGaps:true}))},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{position:"bottom"}},scales:{x:{title:{display:true,text:"hour (UTC)"}},y:{beginAtZero:true}}}});
if(t.stouffer_1_vs_4){const s=t.stouffer_1_vs_4;document.getElementById("stoufferDiv").innerHTML='<b>z = '+s.z.toFixed(2)+'</b>, p = '+s.p.toPrecision(2)+' ('+s.n_strata+' hour strata). '+(s.p<0.001?'The 4-img penalty is highly consistent across hours.':'');}
// tier
if(tier && tier.small){const tiers=["small","medium","large"];new Chart(document.getElementById("tierChart"),{type:"bar",data:{labels:tiers.map(tn=>tn+" (n="+tier[tn].n_authors+")"),datasets:[1,2,3,4].map((ic,i)=>({label:ic+" img",backgroundColor:PC[i],data:tiers.map(tn=>(tier[tn].by_count[ic]||tier[tn].by_count[String(ic)]||{}).mean||0)}))},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{position:"bottom"}},scales:{y:{beginAtZero:true}}}});
let html='<thead><tr><th>Tier</th><th>1 mean</th><th>4 mean</th><th>p</th><th>d</th></tr></thead><tbody>';for(const tn of tiers){const d=tier[tn],d1=d.by_count[1]||d.by_count["1"]||{},d4=d.by_count[4]||d.by_count["4"]||{},mw=d.mw_1_vs_4||{};html+='<tr><td>'+tn+'</td><td>'+(d1.mean||0).toFixed(1)+'</td><td>'+(d4.mean||0).toFixed(1)+'</td><td>'+(mw.p?mw.p.toPrecision(3):"\u2014")+'</td><td>'+(mw.cd!==undefined?(mw.cd>=0?"+":"")+mw.cd.toFixed(3):"\u2014")+'</td></tr>';}document.getElementById("tierTable").innerHTML=html+'</tbody>';}
// text
(function(){const ics=["1","2","3","4"];new Chart(document.getElementById("capChart"),{type:"bar",data:{labels:ics.map(i=>i+" img"),datasets:[{label:"mean caption length",data:ics.map(i=>(text.by_count[i]||text.by_count[Number(i)]).text_len_mean),backgroundColor:PC[0]}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{y:{beginAtZero:true,title:{display:true,text:"characters"}}}}});
let html='<thead><tr><th>Caption</th><th>1 mean</th><th>4 mean</th><th>p</th><th>d</th></tr></thead><tbody>';for(const s of text.caption_stratified_1_vs_4){html+='<tr><td>'+s.bucket+'</td><td>'+s.mean_1.toFixed(1)+'</td><td>'+s.mean_4.toFixed(1)+'</td><td>'+s.mw_p.toPrecision(3)+'</td><td>'+(s.cd>=0?"+":"")+s.cd.toFixed(3)+'</td></tr>';}document.getElementById("capTable").innerHTML=html+'</tbody>';})();
// sumtable
(function(){let html='<thead><tr><th>Group</th><th>n</th><th>mean likes</th><th>median</th></tr></thead><tbody>';html+='<tr><td>1 photo</td><td>'+m.n_single+'</td><td>'+m.metrics.likeCount.single.mean.toFixed(2)+'</td><td>'+m.metrics.likeCount.single.median+'</td></tr>';html+='<tr><td>2+ photos</td><td>'+m.n_multi+'</td><td>'+m.metrics.likeCount.multi.mean.toFixed(2)+'</td><td>'+m.metrics.likeCount.multi.median+'</td></tr>';for(const ic of ["1","2","3","4"]){const L=p.likeCount.by_count[ic];html+='<tr><td>\u2014 '+ic+' img</td><td>'+L.n+'</td><td>'+L.mean.toFixed(2)+'</td><td>'+L.median+'</td></tr>';}document.getElementById("sumTable").innerHTML=html+'</tbody>';})();
</script></body></html>
"""

def write_dashboard(stats_data: dict, title: str, path: Path):
    payload = json.dumps(stats_data, default=str)
    html = DASHBOARD_TEMPLATE.replace("__TITLE__", title).replace("__PAYLOAD__", payload)
    path.write_text(html, encoding="utf-8")

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Analyze engagement (likes/reposts/replies/quotes) by image count for any Bluesky feed.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("feed", help="Feed URL: https://bsky.app/profile/HANDLE/feed/RKEY  or at:// URI  or HANDLE/RKEY")
    ap.add_argument("--target", type=int, default=10000, help="Target number of eligible posts (>=1 image, >24h old). Default 10000.")
    ap.add_argument("--hard-cap", type=int, default=60000, help="Max total posts fetched from the feed. Default 60000.")
    ap.add_argument("--hours", type=float, default=24, help="Exclude posts newer than this many hours. Default 24.")
    ap.add_argument("--output", default="./output", help="Output directory. Default ./output.")
    ap.add_argument("--title", default=None, help="Dashboard title (default = 'HANDLE/RKEY').")
    ap.add_argument("--no-charts", action="store_true", help="Skip rendering PNG charts.")
    ap.add_argument("--no-dashboard", action="store_true", help="Skip generating the interactive HTML dashboard.")
    args = ap.parse_args()

    handle_or_did, rkey = parse_feed_url(args.feed)
    did = resolve_handle(handle_or_did)
    feed_uri = f"at://{did}/app.bsky.feed.generator/{rkey}"
    print(f"[feed] {feed_uri}", file=sys.stderr)

    out = Path(args.output); out.mkdir(parents=True, exist_ok=True)
    state_path = out / ".state.pkl"
    cutoff = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    print(f"[cutoff] posts after {cutoff.isoformat()} will be excluded", file=sys.stderr)

    img_rows = collect(feed_uri, args.target, args.hard_cap, state_path, cutoff)
    if not img_rows:
        print("[error] no eligible posts collected; exiting", file=sys.stderr); sys.exit(1)
    print(f"[collected] {len(img_rows)} image posts", file=sys.stderr)

    stats_data = run_stats(img_rows)
    write_csv(img_rows, out / "feed_engagement.csv")
    (out / "all_stats.json").write_text(json.dumps(stats_data, indent=2, default=str))
    print(f"[wrote] {out/'feed_engagement.csv'}", file=sys.stderr)
    print(f"[wrote] {out/'all_stats.json'}", file=sys.stderr)

    title = args.title or f"{handle_or_did}/{rkey}"
    if not args.no_charts:
        render_charts(stats_data, out / "charts")
    if not args.no_dashboard:
        write_dashboard(stats_data, title, out / "dashboard.html")
        print(f"[wrote] {out/'dashboard.html'}", file=sys.stderr)

    # Print key findings
    m = stats_data["main"]; p = stats_data["per_count"]
    print("\n" + "=" * 60)
    print(f"FEED: {title}")
    print("=" * 60)
    print(f"Sample: {m['n_total']:,} image posts, {m['unique_authors']} authors")
    print(f"Date range: {m['date_range'][0][:10]} \u2192 {m['date_range'][1][:10]}")
    print("\nMean likes by image count:")
    for ic in (1, 2, 3, 4):
        d = p["likeCount"]["by_count"].get(ic, {})
        if d.get("n"):
            print(f"  {ic} img: n={d['n']:>5}  mean={d['mean']:>7.2f}  median={d['median']:>5}")
    print(f"\nKruskal-Wallis (likes): H={p['likeCount']['kruskal_H']:.2f}  p={p['likeCount']['kruskal_p']:.3g}")
    print(f"1 vs 2+ Mann-Whitney (likes): p={m['metrics']['likeCount']['mw_p']:.3g}  Cliff's d={m['metrics']['likeCount']['cd']:+.3f}")

if __name__ == "__main__":
    main()
