# Hosting the desk

The desk is a control surface for something that spends money. Everything
below follows from that one fact, so it is worth stating the threat model
before the commands: **anyone who reaches this port can place trades with your
Kalshi account.** Not "read your positions" — place trades.

That is why the server refuses to bind a public address without a password,
rather than warning about it. A warning is something you scroll past.

---

## The three shapes

| | Reachable from | Credential | Use for |
|---|---|---|---|
| **Local** (default) | this machine only | none needed | development, running it yourself |
| **`--phone`** | your Wi-Fi | URL token | your own phone, behind your own router |
| **Hosted** | the internet | password + sessions | a server you reach from anywhere |
| **`--sandbox`** | anywhere | none by design | a public demo |

`--phone` is not a weaker version of hosting. A URL token is genuinely
adequate behind a home router and genuinely inadequate on the internet: a URL
lands in browser history, in `Referer` headers, in proxy logs, in screenshots,
and in the link you paste when asking someone a question. It also cannot
expire.

---

## Hosted, the short version

**1. Hash your password on your own machine**, so the plaintext never reaches
the host:

```bash
DESK_PASSWORD='a long passphrase you can type' python -m kbot.webui hash-password
# scrypt$32768$8$1$f951...$011b...
```

**2. Put the hash in the host's environment** as `DESK_PASSWORD_HASH`, along
with `MASTER_KEY` and `DESK_TRUST_PROXY=1`.

**3. Put TLS in front of it.** Without TLS the password crosses the network in
the clear and the whole exercise is decorative. `deploy/Caddyfile` does this
and provisions the certificate automatically.

`DESK_PASSWORD` (plaintext) also works and is hashed at startup — it exists
because one-click platforms can only prompt for a plain value, and refusing to
support it would push people toward running with no password at all. Prefer
the hash when you have the choice.

---

## Platform recipes

### Docker Compose

```bash
cp .env.example .env       # add MASTER_KEY and DESK_PASSWORD_HASH
docker compose up -d desk
```

The desk binds `127.0.0.1:8787`; Caddy faces the world. See
`deploy/Caddyfile`.

### fly.io

```bash
fly launch --config fly.desk.toml --no-deploy --copy-config
fly volumes create kbot_desk --size 1
fly secrets set DESK_PASSWORD_HASH='scrypt$...' MASTER_KEY='...'
fly deploy --config fly.desk.toml
```

### Render

`render.yaml` already defines the desk as a web service. Render prompts for
`DESK_PASSWORD` on deploy and generates `MASTER_KEY` itself.

### systemd

```bash
sudo cp deploy/directionalbot-desk.service /etc/systemd/system/
sudo systemctl enable --now directionalbot-desk
```

---

## Environment

| Variable | Meaning |
|---|---|
| `DESK_PASSWORD_HASH` | scrypt hash from `hash-password`. Preferred. |
| `DESK_PASSWORD` | Plaintext, hashed at startup. 12 characters minimum. |
| `DESK_TRUST_PROXY` | `1` when a proxy you control terminates TLS. Enables `X-Forwarded-For` / `X-Forwarded-Proto`, and marks the session cookie `Secure`. |
| `DESK_SANDBOX` | `1` for a public demo. Same as `--sandbox`. |
| `PORT` | Listen port. Honoured for platforms that inject it. |
| `MASTER_KEY` | Encrypts stored Kalshi credentials. **Generate and keep it** — if it changes, connected keys become unreadable. |
| `TRADING_MODE` | `paper` (default), `demo-live`, `production-live`. |
| `ALLOW_PRODUCTION_ORDERS` | Required alongside `TRADING_MODE=production-live`. |

`DESK_TRUST_PROXY` is off by default and that default is load-bearing.
`X-Forwarded-For` is trivially forged by whoever is talking to the server, so
trusting it unconditionally would let an attacker spread login attempts across
imaginary addresses and never trip the lockout. Turn it on only when something
you control is genuinely in front.

---

## What guards what

**Password** — scrypt, ~100 ms and 32 MB per guess. Never stored in plaintext,
compared in constant time.

**Sessions** — server-side, referenced by an `HttpOnly; SameSite=Strict`
cookie, `Secure` when TLS is in play. 12-hour absolute lifetime, 2-hour idle
timeout, id rotated on login so a planted session cannot survive
authentication.

**Lockout** — five wrong guesses buys a minute, doubling to a 15-minute cap.

**CSRF** — a token on every state-changing request, on top of
`SameSite=Strict`. Two mechanisms because the desk moves money.

**Kill switch** — checked immediately before every order submission, and
reachable four ways (env var, file, the UI, Telegram). The file
(`<data dir>/KILL`) works even when the process is wedged.

**Production arming** — two distinct typed phrases, cleared on restart, and
refused outright unless the process was started with both
`TRADING_MODE=production-live` and `ALLOW_PRODUCTION_ORDERS`.

---

## The sandbox

```bash
python -m kbot.webui --sandbox --host 0.0.0.0
```

A sandbox reads **real** Kalshi market data (public, no credentials needed)
and simulates every fill. It is not "paper mode with a banner": the credential
and arming routes return `403` at the HTTP layer, `connect_keys` refuses
before it will even parse a key, and `broker()` cannot construct a live broker
regardless of configuration. Constructing one with `TRADING_MODE=demo-live` or
`production-live` raises rather than silently downgrading, because an operator
who set both has contradictory intentions and guessing which they meant is how
a demo ends up spending money.

That is why it needs no password: there is nothing behind it to protect.

---

## Redeploys

The desk handles `SIGTERM`: it stops the feed, lets an in-flight order finish,
and flushes the intent log before exiting. Give it up to 30 seconds
(`TimeoutStopSec=30` in the systemd unit) rather than `SIGKILL`.

The intent log is why this matters. Every live order is written to disk
*before* it is submitted, so a process killed mid-submission leaves a record
with an unknown outcome that reconciliation can resolve by asking the
exchange. Killing it at the socket instead of letting it drain turns a
recoverable state into a position nobody knows about.

---

## Before you point it at real money

Nothing here makes the strategy profitable, and hosting it does not change
that. No edge has been demonstrated for any strategy in this repo — the
search over 4,000 plans produced results statistically indistinguishable from
shuffled noise on the data available. Host it, watch it, record data. Read
`UPGRADE.md` and `research/CORPUS.md` before arming production.
