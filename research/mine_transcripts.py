"""Mine 445 trading transcripts for concrete, testable rules.

The corpus is ~945k words of speech. Almost all of it is filler, story, and
sales. What is worth extracting is the small fraction that is falsifiable: a
named indicator with a number attached, a timeframe, or a sentence that states
a condition ("never enter when...", "exit if...").

This does no interpretation. It locates and counts, so the reading afterwards
is spent on the passages that carry content rather than on the ones that carry
enthusiasm.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

UPLOADS = Path("/root/.claude/uploads/00bde76d-3fdc-53ed-a598-15bd6e8937df")
OUT = Path("/tmp/claude-0/-home-user-claude-tgb/00bde76d-3fdc-53ed-a598-15bd6e8937df/scratchpad")

# Indicators, with the spellings speech-to-text actually produces.
INDICATORS: dict[str, str] = {
    r"\bvwap\b|\bv-wap\b|volume weighted average": "VWAP",
    r"\bema\b|exponential moving average": "EMA",
    r"\bsma\b|simple moving average": "SMA",
    r"\brsi\b|relative strength": "RSI",
    r"\batr\b|average true range": "ATR",
    r"bollinger": "Bollinger",
    r"\bmacd\b": "MACD",
    r"stochastic": "Stochastic",
    r"fibonacci|\bfib\b": "Fibonacci",
    r"ichimoku": "Ichimoku",
    r"supertrend|super trend": "SuperTrend",
    r"order block|orderblock": "OrderBlock",
    r"fair value gap|\bfvg\b": "FairValueGap",
    r"liquidity (sweep|grab|pool)": "LiquiditySweep",
    r"opening range|\borb\b": "OpeningRange",
    r"pre-?market (high|low)": "PremarketHiLo",
    r"support and resistance|support/resistance": "SupportResistance",
    r"\bvolume profile\b|point of control|\bpoc\b": "VolumeProfile",
    r"\bpivot\b": "Pivot",
    r"keltner": "Keltner",
    r"\badx\b": "ADX",
    r"\bcvd\b|cumulative (volume )?delta": "CVD",
    r"funding rate": "FundingRate",
    r"open interest": "OpenInterest",
    r"\bhammer\b|\bdoji\b|engulfing|\bpin bar\b|\bjohn wick\b": "CandlePattern",
    r"\bdivergence\b": "Divergence",
    r"\bbreak of structure\b|\bbos\b|\bchoch\b": "MarketStructure",
    r"anchored vwap": "AnchoredVWAP",
}

# A sentence is worth reading if it states a condition or a number.
RULE_CUES = re.compile(
    r"\b(never|always|only (?:trade|enter|take)|do not|don't ever|"
    r"wait (?:for|until)|if (?:the )?(?:price|candle|market)|"
    r"enter (?:when|on|if)|exit (?:when|on|if)|stop ?loss|take profit|"
    r"target (?:is|the)|invalidat|"
    r"greater than|less than|above the|below the|more than|at least)\b",
    re.I,
)

NUMERIC = re.compile(
    r"\b(\d{1,4}(?:\.\d+)?)\s*"
    r"(%|percent|cents?|ticks?|pips?|minutes?|min\b|hours?|seconds?|"
    r"period|length|ema|sma|bars?|candles?|r\b|to 1\b)",
    re.I,
)

TIMEFRAME = re.compile(
    r"\b(1|2|3|4|5|10|15|30|45|60|90)\s*[- ]?\s*(second|minute|min|hour|hr)\b"
    r"|\b(daily|weekly|monthly|4h|1h|15m|5m|1m|30m)\b",
    re.I,
)


def parse(path: Path) -> list[dict]:
    """Split a channel dump into {channel, n, title, transcript}."""
    text = path.read_text(encoding="utf-8", errors="replace")
    channel = text.split("\n", 1)[0].lstrip("# ").replace(" - Videos", "").strip()

    # Sections look like:  ## 59. Title   then   ### Transcript   then body.
    parts = re.split(r"(?m)^## (\d+)\.\s*(.+?)\s*$", text)
    videos: list[dict] = []
    for i in range(1, len(parts) - 1, 3):
        n, title, body = parts[i], parts[i + 1], parts[i + 2]
        transcript = ""
        if "### Transcript" in body:
            transcript = body.split("### Transcript", 1)[1]
        videos.append(
            {
                "channel": channel,
                "n": int(n),
                "title": title.strip(),
                "transcript": transcript.strip(),
            }
        )
    return videos


def sentences(text: str) -> list[str]:
    # Transcripts are largely unpunctuated, so split on punctuation *and* on
    # long runs, or a "sentence" becomes the whole video.
    rough = re.split(r"(?<=[.!?])\s+|\n+", text)
    out: list[str] = []
    for chunk in rough:
        words = chunk.split()
        while len(words) > 45:
            out.append(" ".join(words[:45]))
            words = words[45:]
        if words:
            out.append(" ".join(words))
    return out


def main() -> None:
    videos: list[dict] = []
    for path in sorted(UPLOADS.glob("*.md")):
        videos.extend(parse(path))

    print(f"parsed {len(videos)} videos from {len(list(UPLOADS.glob('*.md')))} files")
    with_text = [v for v in videos if len(v["transcript"]) > 400]
    print(f"{len(with_text)} have a usable transcript")

    ind_counts: Counter[str] = Counter()
    ind_videos: defaultdict[str, set] = defaultdict(set)
    cooccur: Counter[tuple[str, str]] = Counter()
    rules: list[dict] = []
    params: defaultdict[str, Counter] = defaultdict(Counter)

    for v in with_text:
        blob = v["transcript"].lower()
        present: set[str] = set()
        for pattern, name in INDICATORS.items():
            hits = len(re.findall(pattern, blob))
            if hits:
                ind_counts[name] += hits
                ind_videos[name].add((v["channel"], v["n"]))
                present.add(name)

        for a in sorted(present):
            for b in sorted(present):
                if a < b:
                    cooccur[(a, b)] += 1

        for s in sentences(v["transcript"]):
            if len(s) < 25:
                continue
            has_rule = bool(RULE_CUES.search(s))
            nums = NUMERIC.findall(s)
            inds = {n for p, n in INDICATORS.items() if re.search(p, s, re.I)}
            if not (inds and (has_rule or nums)):
                continue
            rules.append(
                {
                    "channel": v["channel"],
                    "video": v["n"],
                    "title": v["title"],
                    "indicators": sorted(inds),
                    "text": s.strip(),
                }
            )
            for ind in inds:
                for value, unit in nums:
                    params[ind][f"{value} {unit.lower()}"] += 1

    (OUT / "rules.json").write_text(json.dumps(rules, indent=1))
    print(f"\nextracted {len(rules)} candidate rule sentences -> rules.json")

    print("\n--- indicators by videos mentioning them ---")
    for name, vids in sorted(ind_videos.items(), key=lambda kv: -len(kv[1]))[:24]:
        print(f"  {name:<18} {len(vids):>4} videos   {ind_counts[name]:>5} mentions")

    print("\n--- strongest pairings ---")
    for (a, b), c in cooccur.most_common(14):
        print(f"  {a} + {b:<20} {c}")

    print("\n--- most-repeated numeric parameters ---")
    for ind in ("VWAP", "EMA", "ATR", "RSI", "OpeningRange", "SMA", "Bollinger"):
        top = params[ind].most_common(7)
        if top:
            print(f"  {ind:<14} " + " · ".join(f"{k}({v})" for k, v in top))


if __name__ == "__main__":
    main()
