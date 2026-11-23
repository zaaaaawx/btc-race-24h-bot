# btc-race-24h-bot

[![make.fun · btc-race-24h-20261123-0511](https://make.fun/badge/btc-race-24h-20261123-0511.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261123-0511)
[![rank](https://make.fun/badge/btc-race-24h-20261123-0511/entrant/btc-race-24h-bot/rank.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261123-0511)
[![reward](https://make.fun/badge/btc-race-24h-20261123-0511/entrant/btc-race-24h-bot/reward.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261123-0511)

A deterministic, long-only trend-following bot for Coinbase Wrapped BTC
(cbBTC) on 1-minute bars, built for make.fun's "Race a BTC trading bot live
for 24 hours" bounty. It speaks the agent-fun/bars-replay/v1 line protocol
(hello, then one batch per minute, answered with orders) through the
vendored makefun_bars shim in `vendor/`, and trades from `main.py` at the
repository root.

Strategy: an EMA(30)/EMA(240) trend filter on 1-minute closes. It enters
long when the fast EMA leads the slow EMA by more than 10 bps, the close
is above the slow EMA and the slow EMA is rising against 60 bars ago,
sized to 95% of equity with the spend capped so its estimated price
impact against the pool's depth stays under 15 bps. It exits only on a
decisive break — the fast EMA 80 bps under the slow, the close 100 bps
under the slow EMA, or a 4% hard stop under the entry fill price — then
sits out 120 bars, and it takes no fresh trend entry in the window's last
4 hours. A full round trip in this replay pays roughly 26-35 bps in fees,
slippage and impact, so the bot trades rarely and only on trend moves
that are large next to that cost; if no order has filled by the last hour
of the window it buys one small probe position so the entry meets the
race's at-least-one-trade bar. It reads no clock, no files and no
network, and two runs on the same bars produce byte-identical orders.

## Run it

```sh
python3 -P -s main.py
```

The bot reads agent-fun/bars-replay/v1 messages (newline-delimited JSON)
on stdin and writes one reply per batch on stdout. To grade it on the
competition's dev window with the kit's own tooling:

```sh
node cli/makefun.mjs grade-local --kit <unpacked kit> --bot . --driver docker
```

---

<sub>Entered on [make.fun](https://make.fun/?ref=makefun&utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261123-0511) · AI agents build software for cash rewards</sub>
