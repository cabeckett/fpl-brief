#!/usr/bin/env python3
"""
fpl.py - Fantasy Premier League decision support.

Runs entirely on your machine. Pulls the public FPL API, scores every player
over a fixture horizon, and writes a markdown brief you can paste into Claude.

Commands
--------
  python fpl.py build                 Optimise an initial 15-man squad
  python fpl.py week --team 1234567   Transfers, captain and chip advice
  python fpl.py refresh               Force re-download of cached data

Optional: to get true selling prices, bank and free transfers, set FPL_COOKIE
to your own pl_profile/sessionid cookie string. It stays on your machine and is
never printed into the brief. Everything works without it, just less precisely.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from itertools import combinations
from pathlib import Path

BASE = "https://fantasy.premierleague.com/api"
CACHE = Path(os.environ.get("FPL_CACHE", Path.home() / ".fpl_cache"))
CACHE_TTL = 60 * 60 * 3  # 3 hours

POS = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
SQUAD_QUOTA = {1: 2, 2: 5, 3: 5, 4: 3}
BUDGET = 1000  # tenths of a million
MAX_PER_CLUB = 3

# Formation limits for the starting XI
XI_MIN = {1: 1, 2: 3, 3: 2, 4: 1}
XI_MAX = {1: 1, 2: 5, 3: 5, 4: 3}

# Fixture difficulty -> scoring multiplier
FDR_ATT = {1: 1.22, 2: 1.11, 3: 1.00, 4: 0.89, 5: 0.79}
FDR_DEF = {1: 1.34, 2: 1.16, 3: 1.00, 4: 0.85, 5: 0.72}

# Bench is worth something, but not much. Weight for optimiser objective.
BENCH_WEIGHT = 0.12


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

def _get(path: str, ttl: int = CACHE_TTL, auth: bool = False) -> dict:
    CACHE.mkdir(parents=True, exist_ok=True)
    key = path.strip("/").replace("/", "_") + ".json"
    fp = CACHE / key

    if not auth and fp.exists() and time.time() - fp.stat().st_mtime < ttl:
        return json.loads(fp.read_text())

    req = urllib.request.Request(
        f"{BASE}/{path.lstrip('/')}",
        headers={"User-Agent": "Mozilla/5.0 (fpl.py)"},
    )
    cookie = os.environ.get("FPL_COOKIE")
    if auth and cookie:
        req.add_header("Cookie", cookie)
        req.add_header("X-Requested-With", "XMLHttpRequest")
        req.add_header("Referer", "https://fantasy.premierleague.com/")

    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} fetching {path}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error fetching {path}: {e.reason}") from e

    if not auth:
        fp.write_text(json.dumps(data))
    return data


def bootstrap() -> dict:
    return _get("bootstrap-static/")


def fixtures() -> list:
    return _get("fixtures/")


def player_history(pid: int) -> dict:
    return _get(f"element-summary/{pid}/", ttl=60 * 60 * 24 * 7)


# --------------------------------------------------------------------------
# fixture model
# --------------------------------------------------------------------------

def current_gw(bs: dict) -> int:
    for ev in bs["events"]:
        if ev.get("is_next"):
            return ev["id"]
    for ev in bs["events"]:
        if not ev.get("finished"):
            return ev["id"]
    return bs["events"][-1]["id"]


def fixture_map(fx: list, start_gw: int, horizon: int) -> dict:
    """{team_id: {gw: [difficulty, ...]}} for the next `horizon` gameweeks."""
    out: dict = {}
    window = range(start_gw, start_gw + horizon)
    for f in fx:
        gw = f.get("event")
        if gw is None or gw not in window:
            continue
        out.setdefault(f["team_h"], {}).setdefault(gw, []).append(f["team_h_difficulty"])
        out.setdefault(f["team_a"], {}).setdefault(gw, []).append(f["team_a_difficulty"])
    return out


def blank_and_double(fmap: dict, teams: list, start_gw: int, horizon: int) -> dict:
    """Flag gameweeks where teams blank (0 fixtures) or double (2+)."""
    names = {t["id"]: t["short_name"] for t in teams}
    report: dict = {}
    for gw in range(start_gw, start_gw + horizon):
        blanks = [names[t["id"]] for t in teams if len(fmap.get(t["id"], {}).get(gw, [])) == 0]
        doubles = [names[t["id"]] for t in teams if len(fmap.get(t["id"], {}).get(gw, [])) >= 2]
        if blanks or doubles:
            report[gw] = {"blank": sorted(blanks), "double": sorted(doubles)}
    return report


# --------------------------------------------------------------------------
# player scoring
# --------------------------------------------------------------------------

def _price_prior(p: dict, floor: dict) -> float:
    """Crude points-per-90 prior from price. Used before real minutes exist."""
    over = (p["now_cost"] - floor[p["element_type"]]) / 10.0
    base = {1: 3.1, 2: 3.0, 3: 2.4, 4: 2.5}[p["element_type"]]
    slope = {1: 0.35, 2: 0.42, 3: 0.60, 4: 0.55}[p["element_type"]]
    return base + over * slope


def _prev_season(pid: int) -> tuple | None:
    try:
        past = player_history(pid).get("history_past", [])
    except RuntimeError:
        return None
    if not past:
        return None
    last = past[-1]
    return last.get("minutes", 0), last.get("total_points", 0)


def score_players(bs: dict, fmap: dict, start_gw: int, horizon: int,
                  deep: bool = False) -> list:
    """Attach a projected-points score over the horizon to every player."""
    elements = bs["elements"]
    played = sum(1 for e in bs["events"] if e.get("finished"))

    floor = {}
    for et in POS:
        costs = [e["now_cost"] for e in elements if e["element_type"] == et]
        floor[et] = min(costs) if costs else 40

    # Deep mode pulls last season's numbers for a shortlist of candidates.
    prev: dict = {}
    if deep:
        shortlist = sorted(elements, key=lambda e: -e["now_cost"])[:260]
        for i, e in enumerate(shortlist, 1):
            if i % 40 == 0:
                print(f"  ...history {i}/{len(shortlist)}", file=sys.stderr)
            r = _prev_season(e["id"])
            if r:
                prev[e["id"]] = r

    scored = []
    for p in elements:
        et = p["element_type"]
        mins = float(p["minutes"])
        pts = float(p["total_points"])

        # points per 90
        if mins >= 270:
            per90 = pts / mins * 90.0
            confidence = "season"
        elif p["id"] in prev and prev[p["id"]][0] >= 900:
            pm, pp = prev[p["id"]]
            per90 = pp / pm * 90.0
            confidence = "last-season"
        else:
            per90 = _price_prior(p, floor)
            confidence = "price-prior"

        # blend toward the prior when evidence is thin
        prior = _price_prior(p, floor)
        if confidence != "price-prior":
            w = min(1.0, mins / 900.0) if confidence == "season" else 0.7
            per90 = w * per90 + (1 - w) * prior

        # expected minutes share
        if played >= 3 and mins > 0:
            share = min(1.0, mins / (90.0 * played))
        elif p["id"] in prev:
            share = min(1.0, prev[p["id"]][0] / (90.0 * 38))
        else:
            share = 0.55 + 0.35 * (p["now_cost"] - floor[et]) / max(1, (140 - floor[et]))
            share = min(0.95, share)

        # availability
        status = p.get("status", "a")
        chance = p.get("chance_of_playing_next_round")
        avail = 1.0
        if status in ("i", "s", "u", "n"):
            avail = 0.0
        elif status == "d":
            avail = (chance if chance is not None else 50) / 100.0
        if chance == 0:
            avail = 0.0

        tbl = FDR_DEF if et in (1, 2) else FDR_ATT
        gw_scores = []
        total = 0.0
        for gw in range(start_gw, start_gw + horizon):
            diffs = fmap.get(p["team"], {}).get(gw, [])
            gw_pts = sum(per90 * share * avail * tbl.get(d, 1.0) for d in diffs)
            gw_scores.append(round(gw_pts, 2))
            total += gw_pts

        scored.append({
            "id": p["id"],
            "name": p["web_name"],
            "team": p["team"],
            "pos": et,
            "cost": p["now_cost"],
            "score": round(total, 2),
            "next_gw": gw_scores[0] if gw_scores else 0.0,
            "per90": round(per90, 2),
            "share": round(share, 2),
            "avail": avail,
            "status": status,
            "news": p.get("news", ""),
            "owned": float(p.get("selected_by_percent", 0) or 0),
            "form": float(p.get("form", 0) or 0),
            "confidence": confidence,
            "gw_scores": gw_scores,
        })
    return scored


# --------------------------------------------------------------------------
# squad optimisation
# --------------------------------------------------------------------------

def best_xi(squad: list, key: str = "next_gw") -> tuple:
    """Return (xi, bench, total) maximising `key` under formation rules."""
    by_pos = {et: sorted([p for p in squad if p["pos"] == et],
                         key=lambda x: -x[key]) for et in POS}
    best = (None, None, -1.0)
    for ndef in range(XI_MIN[2], XI_MAX[2] + 1):
        for nmid in range(XI_MIN[3], XI_MAX[3] + 1):
            nfwd = 10 - ndef - nmid
            if not (XI_MIN[4] <= nfwd <= XI_MAX[4]):
                continue
            picks = (by_pos[1][:1] + by_pos[2][:ndef]
                     + by_pos[3][:nmid] + by_pos[4][:nfwd])
            if len(picks) != 11:
                continue
            tot = sum(p[key] for p in picks)
            if tot > best[2]:
                ids = {p["id"] for p in picks}
                bench = [p for p in squad if p["id"] not in ids]
                best = (picks, bench, tot)
    return best


def squad_objective(squad: list) -> float:
    xi, bench, tot = best_xi(squad, key="score")
    if xi is None:
        return -1e9
    return tot + BENCH_WEIGHT * sum(p["score"] for p in bench)


def _valid(squad: list) -> bool:
    if sum(p["cost"] for p in squad) > BUDGET:
        return False
    counts: dict = {}
    for p in squad:
        counts[p["team"]] = counts.get(p["team"], 0) + 1
        if counts[p["team"]] > MAX_PER_CLUB:
            return False
    return True


def build_squad(pool: list, iterations: int = 4000) -> list:
    """Greedy seed by value, then hill-climb single swaps."""
    pool = [p for p in pool if p["avail"] > 0.4]
    pool.sort(key=lambda p: -(p["score"] / max(1, p["cost"])))

    cheap_by_pos = {et: sorted(x["cost"] for x in pool if x["pos"] == et)
                    for et in POS}

    squad: list = []
    need = dict(SQUAD_QUOTA)
    club: dict = {}
    for p in pool:
        if need[p["pos"]] == 0 or club.get(p["team"], 0) >= MAX_PER_CLUB:
            continue
        trial = squad + [p]
        # cost of filling every remaining slot with the cheapest legal option
        floor_cost = 0
        for et, n in need.items():
            k = n - (1 if et == p["pos"] else 0)
            if k > 0:
                floor_cost += sum(cheap_by_pos[et][:k])
        if sum(x["cost"] for x in trial) + floor_cost > BUDGET:
            continue
        squad = trial
        need[p["pos"]] -= 1
        club[p["team"]] = club.get(p["team"], 0) + 1
        if sum(need.values()) == 0:
            break

    if len(squad) < 15:
        raise RuntimeError("Could not assemble a legal 15 from the pool.")
    if not _valid(squad):
        raise RuntimeError("Greedy seed is illegal — this is a bug, please report.")

    # hill climb
    current = squad_objective(squad)
    ids = {p["id"] for p in squad}
    top_pool = pool[:450]
    for _ in range(iterations):
        improved = False
        for out in list(squad):
            for inc in top_pool:
                if inc["id"] in ids or inc["pos"] != out["pos"]:
                    continue
                trial = [x for x in squad if x["id"] != out["id"]] + [inc]
                if not _valid(trial):
                    continue
                val = squad_objective(trial)
                if val > current + 1e-6:
                    squad, current = trial, val
                    ids = {p["id"] for p in squad}
                    improved = True
                    break
            if improved:
                break
        if not improved:
            break
    return squad


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def fmt_money(t: int) -> str:
    return f"£{t / 10:.1f}m"


COMPACT = False


def render_squad(squad: list, teams: dict) -> str:
    xi, bench, _ = best_xi(squad, key="next_gw")
    lines = [f"**Cost:** {fmt_money(sum(p['cost'] for p in squad))} "
             f"/ {fmt_money(BUDGET)}\n"]

    if COMPACT:
        for et in (1, 2, 3, 4):
            group = [p for p in xi if p["pos"] == et]
            if not group:
                continue
            lines.append(f"**{POS[et]}**")
            for p in sorted(group, key=lambda x: -x["next_gw"]):
                lines.append(f"- {p['name']} · {teams[p['team']]} · "
                             f"{fmt_money(p['cost'])} · {p['next_gw']:.1f}")
        lines.append("**Bench**")
        for p in sorted(bench, key=lambda x: (x["pos"] == 1, -x["next_gw"])):
            lines.append(f"- {p['name']} · {teams[p['team']]} · "
                         f"{fmt_money(p['cost'])} · {p['next_gw']:.1f}")
        return "\n".join(lines)

    lines.append("| | Player | Club | Pos | Price | Next GW | Horizon |")
    lines.append("|---|---|---|---|---|---|---|")
    for p in sorted(xi, key=lambda x: (x["pos"], -x["next_gw"])):
        lines.append(f"| XI | {p['name']} | {teams[p['team']]} | {POS[p['pos']]} "
                     f"| {fmt_money(p['cost'])} | {p['next_gw']:.1f} | {p['score']:.1f} |")
    for p in sorted(bench, key=lambda x: (x["pos"], -x["next_gw"])):
        lines.append(f"| bench | {p['name']} | {teams[p['team']]} | {POS[p['pos']]} "
                     f"| {fmt_money(p['cost'])} | {p['next_gw']:.1f} | {p['score']:.1f} |")
    return "\n".join(lines)


def captain_table(xi: list, teams: dict, n: int = 5) -> str:
    ranked = sorted(xi, key=lambda p: -p["next_gw"])[:n]
    if COMPACT:
        return "\n".join(
            f"{i}. {p['name']} ({teams[p['team']]}) — {p['next_gw']:.1f}, {p['owned']:.0f}%"
            for i, p in enumerate(ranked, 1))
    lines = ["| Rank | Player | Club | Next GW | Owned |", "|---|---|---|---|---|"]
    for i, p in enumerate(ranked, 1):
        lines.append(f"| {i} | {p['name']} | {teams[p['team']]} | "
                     f"{p['next_gw']:.1f} | {p['owned']:.1f}% |")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_build(args) -> str:
    bs = bootstrap()
    fx = fixtures()
    gw = args.gw or current_gw(bs)
    fmap = fixture_map(fx, gw, args.horizon)
    teams = {t["id"]: t["short_name"] for t in bs["teams"]}

    print(f"Scoring players for GW{gw}..GW{gw + args.horizon - 1}", file=sys.stderr)
    pool = score_players(bs, fmap, gw, args.horizon, deep=args.deep)
    squad = build_squad(pool)
    xi, bench, _ = best_xi(squad, key="next_gw")

    out = [f"# FPL squad build — GW{gw}", ""]
    out.append(f"Horizon: {args.horizon} gameweeks. "
               f"Evidence: {'last-season history' if args.deep else 'price priors + current form'}.")
    out.append("")
    out.append(render_squad(squad, teams))
    out.append("")
    out.append("## Captain shortlist")
    out.append(captain_table(xi, teams))
    out.append("")
    out.append("## Bench order")
    for i, p in enumerate(sorted([b for b in bench if b["pos"] != 1],
                                 key=lambda x: -x["next_gw"]), 1):
        out.append(f"{i}. {p['name']} ({teams[p['team']]}) — {p['next_gw']:.1f}")
    out.append("")
    out.append("## Near misses (next best by value)")
    ids = {p["id"] for p in squad}
    misses = [p for p in pool if p["id"] not in ids and p["avail"] > 0.6]
    misses.sort(key=lambda p: -(p["score"] / max(1, p["cost"])))
    for p in misses[:12]:
        out.append(f"- {p['name']} ({teams[p['team']]}, {POS[p['pos']]}, "
                   f"{fmt_money(p['cost'])}) — horizon {p['score']:.1f}")
    return "\n".join(out)


def _my_team(team_id: int, gw: int) -> dict | None:
    """Authenticated endpoint: selling prices, bank, free transfers, chips."""
    if not os.environ.get("FPL_COOKIE"):
        return None
    try:
        return _get(f"my-team/{team_id}/", auth=True)
    except RuntimeError as e:
        print(f"  (auth endpoint unavailable: {e})", file=sys.stderr)
        return None


def cmd_week(args) -> str:
    bs = bootstrap()
    fx = fixtures()
    gw = args.gw or current_gw(bs)
    fmap = fixture_map(fx, gw, args.horizon)
    teams = {t["id"]: t["short_name"] for t in bs["teams"]}
    pool = score_players(bs, fmap, gw, args.horizon, deep=args.deep)
    by_id = {p["id"]: p for p in pool}

    entry = _get(f"entry/{args.team}/", ttl=600)
    mine = _my_team(args.team, gw)

    bank = None
    free_transfers = None
    picks_ids = []
    selling = {}

    if mine:
        picks_ids = [pk["element"] for pk in mine["picks"]]
        selling = {pk["element"]: pk["selling_price"] for pk in mine["picks"]}
        bank = mine["transfers"]["bank"]
        free_transfers = mine["transfers"].get("limit")
    else:
        # fall back to last completed gameweek's public picks
        for g in range(gw - 1, 0, -1):
            try:
                pk = _get(f"entry/{args.team}/event/{g}/picks/", ttl=600)
                picks_ids = [x["element"] for x in pk["picks"]]
                break
            except RuntimeError:
                continue
        bank = entry.get("last_deadline_bank")

    if not picks_ids:
        return ("Could not read your squad. If the season hasn't started, run "
                "`python fpl.py build` instead.")

    squad = [by_id[i] for i in picks_ids if i in by_id]
    for p in squad:
        p["sell"] = selling.get(p["id"], p["cost"])

    xi, bench, _ = best_xi(squad, key="next_gw")
    bank = bank if bank is not None else 0

    out = [f"# FPL week brief — GW{gw}", ""]
    rank = entry.get("summary_overall_rank")
    header = f"**{entry.get('name', '?')}**"
    if isinstance(rank, int):
        header += f" · overall rank {rank:,}"
    out.append(header)
    out.append(f"Bank {fmt_money(bank)}"
               + (f" · {free_transfers} free transfer(s)" if free_transfers else "")
               + ("" if mine else "  \n_(no cookie set — prices are current, not your selling prices)_"))
    out.append("")

    # flags
    flags = [p for p in squad if p["avail"] < 0.75]
    if flags:
        out.append("## Flagged")
        for p in flags:
            note = p["news"] or f"status {p['status']}"
            out.append(f"- **{p['name']}** ({teams[p['team']]}) — {note}")
        out.append("")

    out.append("## Current squad")
    out.append(render_squad(squad, teams))
    out.append("")

    # single transfers
    out.append("## Transfer options (1 move)")
    ids = {p["id"] for p in squad}
    club_count: dict = {}
    for p in squad:
        club_count[p["team"]] = club_count.get(p["team"], 0) + 1

    ideas = []
    for out_p in squad:
        budget = bank + out_p.get("sell", out_p["cost"])
        for in_p in pool:
            if in_p["id"] in ids or in_p["pos"] != out_p["pos"]:
                continue
            if in_p["cost"] > budget or in_p["avail"] < 0.6:
                continue
            if in_p["team"] != out_p["team"] and \
               club_count.get(in_p["team"], 0) >= MAX_PER_CLUB:
                continue
            trial = [x for x in squad if x["id"] != out_p["id"]] + [in_p]
            gain = squad_objective(trial) - squad_objective(squad)
            if gain > 0:
                ideas.append((gain, out_p, in_p, budget - in_p["cost"]))
    ideas.sort(key=lambda x: -x[0])

    if ideas:
        out.append("| Out | In | Gain (horizon) | Bank after |")
        out.append("|---|---|---|---|")
        for gain, o, i, left in ideas[:8]:
            out.append(f"| {o['name']} ({teams[o['team']]}) | {i['name']} "
                       f"({teams[i['team']]}) | +{gain:.1f} | {fmt_money(left)} |")
    else:
        out.append("No single transfer improves the squad over the horizon. Roll it.")
    out.append("")

    out.append("## Captain")
    out.append(captain_table(xi, teams))
    out.append("")

    out.append("## Bench order")
    for i, p in enumerate(sorted([b for b in bench if b["pos"] != 1],
                                 key=lambda x: -x["next_gw"]), 1):
        out.append(f"{i}. {p['name']} ({teams[p['team']]}) — {p['next_gw']:.1f}")
    out.append("")

    # chips
    out.append("## Chip watch")
    bd = blank_and_double(fmap, bs["teams"], gw, args.horizon)
    if bd:
        for g, info in sorted(bd.items()):
            bits = []
            if info["double"]:
                bits.append("doubles: " + ", ".join(info["double"]))
            if info["blank"]:
                bits.append("blanks: " + ", ".join(info["blank"]))
            out.append(f"- **GW{g}** — " + " · ".join(bits))
    else:
        out.append("- No blanks or doubles in the horizon.")

    bench_pts = sum(p["next_gw"] for p in bench)
    out.append(f"- Bench Boost value this week: **{bench_pts:.1f} pts** "
               f"({'worth considering' if bench_pts >= 14 else 'hold'})")
    if xi:
        top = max(xi, key=lambda p: p["next_gw"])
        out.append(f"- Triple Captain on {top['name']} would add "
                   f"**{top['next_gw']:.1f} pts** "
                   f"({'strong' if top['next_gw'] >= 8 else 'wait for a better week'})")
    if len(flags) >= 4 or (ideas and len(ideas) > 0 and ideas[0][0] > 12):
        out.append("- Squad has multiple problems — wildcard is on the table.")
    return "\n".join(out)


def cmd_refresh(args) -> str:
    if CACHE.exists():
        for f in CACHE.glob("*.json"):
            f.unlink()
    bootstrap()
    fixtures()
    return f"Cache cleared and refreshed at {CACHE}"


# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="FPL decision support")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="optimise an initial 15-man squad")
    b.add_argument("--horizon", type=int, default=6)
    b.add_argument("--gw", type=int)
    b.add_argument("--deep", action="store_true",
                   help="pull last season's per-player history (slower, better)")
    b.add_argument("--compact", action="store_true",
                   help="phone-friendly output (lists instead of wide tables)")
    b.add_argument("-o", "--out", default="fpl_brief.md")
    b.set_defaults(fn=cmd_build)

    w = sub.add_parser("week", help="weekly transfers, captain and chips")
    w.add_argument("--team", type=int, required=True, help="your FPL entry ID")
    w.add_argument("--horizon", type=int, default=5)
    w.add_argument("--gw", type=int)
    w.add_argument("--deep", action="store_true")
    w.add_argument("--compact", action="store_true",
                   help="phone-friendly output (lists instead of wide tables)")
    w.add_argument("-o", "--out", default="fpl_brief.md")
    w.set_defaults(fn=cmd_week)

    r = sub.add_parser("refresh", help="clear and refetch cached data")
    r.add_argument("-o", "--out", default=None)
    r.set_defaults(fn=cmd_refresh)

    args = ap.parse_args()
    global COMPACT
    COMPACT = getattr(args, "compact", False)
    try:
        text = args.fn(args)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    print(text)
    if getattr(args, "out", None):
        Path(args.out).write_text(text)
        print(f"\n[written to {args.out}]", file=sys.stderr)


if __name__ == "__main__":
    main()
