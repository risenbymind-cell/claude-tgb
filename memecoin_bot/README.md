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
