# Confirmed-Flow Momentum — Pump.fun Replay Sprint Paper Bot

[![make.fun bounty](https://make.fun/badge/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122.svg)](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122)
[![rank](https://make.fun/badge/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/cendelobening/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/rank.svg)](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122)
[![reward](https://make.fun/badge/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/cendelobening/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/reward.svg)](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122)

Entry for the make.fun bounty [Pump.fun Replay Sprint — Paper Bot, Real Ranking](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122).

## Strategy

Confirmed-flow momentum with fast exits, over the agent-fun/replay/v2
protocol: a `hello` message (capital, caps, fees, seed), then one `batch`
per slot decision point, with orders executing two slots after the batch
that decided them.

- **Watch the flow.** For every mint the bot folds each batch's trade
  events into a recent window: trade count, buy vs sell quote volume, and
  the real quote reserve at each batch close.
- **Enter only on confirmation.** A coin qualifies when it is young in
  slot age, has printed at least 8 trades in the window, buys make up at
  least 70% of window quote volume, reserves grew across the last two
  batches, and the size cap (`state.max_buy_lamports`) allows at least
  0.02 SOL. It then stakes a fixed 0.05 SOL (never more than the cap),
  at most 2 new buys per batch and at most 5 open positions, keeping a
  0.10 SOL cash reserve back so sell fees never fail.
- **Exit fast.** The whole position is sold at +20% mark gain
  (take-profit), cut at −12% (stop-loss), or time-stopped after 1,200
  slots so capital recycles across many distinct coins. Buys use 2000 bps
  slippage; exits use 9000 bps because a filled exit beats a perfect price.
- **Late window guard.** A 24h replay spans roughly 216,000 slots;
  entries stop in the last 3,000 slots because leftovers are flattened at
  the final marks.

The bot is fully deterministic: no clock, no environment reads, no
randomness — the same hello and batches always produce byte-identical
orders.

## Layout

- `main.py` — the strategy (Python standard library only).
- `vendor/makefun_bot.py` — the kit's agent-fun/replay/v2 shim, vendored
  unmodified as the SDK instructs.
