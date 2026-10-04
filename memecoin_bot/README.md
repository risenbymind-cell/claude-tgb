# Memecoin paper bot (Solana)

Rules-based **paper** trader. Reads public DexScreener data, never touches a wallet.

    pip install -r requirements.txt
    python bot.py            # loops every 30s; state.json + journal.csv are written here
    python bot.py --once     # single pass
    pytest

Rules (all in `Config`): vet before buying (liquidity, age >10m, buy/sell ratio,
no chasing pumps, fake-volume check, FDV cap); 2% of bankroll per trade, max 3
positions, 50% exposure; refuse fills with >5% slippage; -25% stop, sell half at
+100%, 30% trailing stop on the rest, 30-min time stop; stop opening trades after
-10% on the day; 20-min cooldown after a stop (no revenge trades).

Not financial advice. Most meme coins go to zero; DexScreener data can be delayed
and cannot show rugs that the checklist can't see (mint/freeze authority, bundles).

## Dashboard

    python bot.py          # terminal 1: writes state.json + journal.csv
    python dashboard.py    # terminal 2: http://127.0.0.1:8000

`site/index.html` is a static page (no build step) showing equity, P&L, the equity
curve, open positions with their stop/TP levels, the rules in force, and the journal.
`dashboard.py` is read-only and binds to localhost only. To host the page elsewhere,
serve `site/` and put `state.json` and `journal.csv` next to it.
