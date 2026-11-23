"""A trend-following bot for make.fun's "Race a BTC trading bot live for 24 hours".

It speaks agent-fun/bars-replay/v1 through the vendored makefun_bars shim:
the harness sends hello once, then one batch per minute carrying that
minute's bar, the fills of the orders decided on the previous batch and the
account; the bot answers each batch with one reply holding its orders.
Orders decided on a batch fill at the next bar's open, paying a 4 bps fee,
2 bps of modelled slippage and constant-product price impact against that
bar's pool depth, so a full round trip costs roughly 26-35 bps: the strategy
below only trades moves that are large next to that cost.

Strategy (long only, at most one position):
  * Trend filter on 1-minute closes: EMA(30) against EMA(240), with a
    hysteresis band so chop around the cross does not churn orders.
  * Enter when the fast EMA is above the slow EMA by more than the entry
    band, the close itself is above the slow EMA, and the slow EMA is
    rising against 60 bars ago: counter-rally entries in a falling market
    are what a trend filter must veto, and a rising slow line is the veto.
  * Exit when the fast EMA falls below the slow EMA by a much wider band,
    when the close falls well below the slow EMA, or on a 4% hard stop
    under the entry fill price. The exit bands are deliberately wide and
    the re-entry cooldown long: in testing, exits keyed tightly to either
    line churned through the sideways hours at a full round-trip cost
    each, so the bot only leaves on a decisive break and otherwise holds
    to the end. No fresh trend entry is taken in the window's last
    4 hours, when too little time remains to earn back a round trip.
  * Size is 95% of equity, capped so the estimated price impact against
    the latest close's pool depth stays at or under 15 bps, and never
    below the harness's minimum notional.
  * A 120-bar cooldown after an exit fill before any re-entry.
  * Guardrail probe: the race ranks an entry below every bar-meeting entry
    unless it made at least one trade, so if no order has filled by the
    last hour of the window the bot buys one small probe position and lets
    the end-of-window flatten close it.

Determinism: decisions use only the closes and fills the harness sent, in
arrival order, with integer arithmetic and fixed-order float EMA updates.
No clock, no randomness, no files, no network.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bars

# --- strategy parameters (bars are 1 minute) ---
FAST_SPAN = 30            # fast EMA span
SLOW_SPAN = 240           # slow EMA span
MIN_HISTORY = 150         # closes wanted before the first entry
BAND_BPS = 10             # entry band: fast EMA must lead the slow by this
SLOPE_BARS = 60           # the slow EMA must be rising against this long ago
EXIT_BAND_BPS = 80        # exit band for the fast/slow cross, in bps
CLOSE_BAND_BPS = 100      # exit band for the close against the slow EMA
STOP_BPS = 400            # hard stop under the entry fill price, in bps
TARGET_BPS = 9500         # share of equity a full entry spends, in bps
IMPACT_CAP_BPS = 15       # estimated price impact an entry may pay, in bps
COOLDOWN_BARS = 120       # bars to sit out after an exit fill
ENTRY_CUTOFF_BAR = 1200   # no fresh trend entries in the window's last 4 hours
SLIPPAGE_BPS = 200        # max_slippage_bps sent with trend orders
PROBE_AFTER_BAR = 1380    # scored bar from which the guardrail probe may fire
PROBE_NOTIONAL = 30_000_000  # the probe's size: 30 USDC in quote units
PROBE_SLIPPAGE_BPS = 300  # max_slippage_bps sent with the probe


class Bot:
    """The strategy's whole state, folded batch by batch."""

    def __init__(self):
        self.symbol = "CBBTC"
        self.closes = []          # every CBBTC close seen, in order
        self.ema_fast = None
        self.ema_slow = None
        self.slow_history = []    # the slow EMA after each bar, for its slope
        self.pending_id = None    # client_id of the order in flight, if any
        self.entry_price = None   # fill price of the open position's buy
        self.filled_trades = 0    # own orders that filled (flattens excluded)
        self.cooldown_until = -1  # scored bars before this one stay out after an exit
        self.warmup_bars = 0      # hello's warm-up length, learned on batch 0

    # -- bookkeeping -----------------------------------------------------

    def note_fills(self, batch):
        """Fold this batch's fills in: they settle the order sent last batch."""
        for fill in batch["fills"]:
            cid = fill.get("client_id", "")
            if cid.startswith("_"):
                continue  # the replay's own flatten closes are not our trades
            if self.pending_id is not None and cid == self.pending_id:
                self.pending_id = None
            if fill.get("status") != "filled":
                continue
            self.filled_trades += 1
            if fill.get("side") == "buy":
                self.entry_price = int(fill["price"])
            elif fill.get("side") == "sell":
                self.entry_price = None
                self.cooldown_until = self.scored_bar(batch) + COOLDOWN_BARS

    def note_bar(self, batch):
        """Fold this batch's CBBTC bar in; True when a fresh bar arrived."""
        fresh = False
        for bar in batch["bars"]:
            if bar["symbol"] != self.symbol:
                continue
            close = int(bar["c"])
            self.closes.append(close)
            self.ema_fast = self._ema(self.ema_fast, close, FAST_SPAN)
            self.ema_slow = self._ema(self.ema_slow, close, SLOW_SPAN)
            self.slow_history.append(self.ema_slow)
            fresh = True
        return fresh

    @staticmethod
    def _ema(prev, close, span):
        if prev is None:
            return float(close)
        alpha = 2.0 / (span + 1)
        return prev + alpha * (close - prev)

    def scored_bar(self, batch):
        """Bars of the scored window answered so far, warm-up excluded."""
        return batch["seq"] - self.warmup_bars

    # -- decisions ---------------------------------------------------------

    def on_batch(self, state, batch):
        if state.seq == 0:
            self.warmup_bars = state.warmup_bars
            if state.hello["symbols"]:
                self.symbol = state.hello["symbols"][0]["symbol"]
        self.note_fills(batch)
        fresh = self.note_bar(batch)
        if state.warming_up or not fresh or self.pending_id is not None:
            return []
        held = state.positions.get(self.symbol)
        if held is not None and held.qty > 0:
            return self._exit_orders(state)
        return self._entry_orders(state, batch)

    def _exit_orders(self, state):
        held = state.positions[self.symbol]
        close = self.closes[-1]
        stop_hit = (
            self.entry_price is not None
            and close * 10_000 < self.entry_price * (10_000 - STOP_BPS)
        )
        trend_off = False
        if self.ema_fast is not None and self.ema_slow is not None:
            cross_off = self.ema_fast * 10_000 < self.ema_slow * (10_000 - EXIT_BAND_BPS)
            close_off = close * 10_000 < int(self.ema_slow) * (10_000 - CLOSE_BAND_BPS)
            trend_off = cross_off or close_off
        if not (stop_hit or trend_off):
            return []
        order = {
            "side": "sell",
            "client_id": state.client_id(),
            "symbol": self.symbol,
            "qty": held.qty,
            "max_slippage_bps": SLIPPAGE_BPS,
        }
        self.pending_id = order["client_id"]
        return [order]

    def _entry_orders(self, state, batch):
        scored = self.scored_bar(batch)
        # The guardrail probe: no filled trade yet and the window is nearly
        # over, so buy a small position whatever the trend says.
        if self.filled_trades == 0 and scored >= PROBE_AFTER_BAR:
            return self._buy(state, min(state.cash, PROBE_NOTIONAL), PROBE_SLIPPAGE_BPS)
        if len(self.closes) < MIN_HISTORY or scored < self.cooldown_until:
            return []
        if scored >= ENTRY_CUTOFF_BAR:
            return []
        band = 1.0 + BAND_BPS / 10_000.0
        rising = (
            len(self.slow_history) > SLOPE_BARS
            and self.ema_slow > self.slow_history[-1 - SLOPE_BARS]
        )
        trend_up = (
            rising
            and self.ema_fast > self.ema_slow * band
            and self.closes[-1] > self.ema_slow
        )
        if not trend_up:
            return []
        notional = state.equity * TARGET_BPS // 10_000
        return self._buy(state, notional, SLIPPAGE_BPS)

    def _buy(self, state, notional, slippage_bps):
        notional = min(notional, state.cash, state.weight_room(self.symbol))
        min_notional = int(state.hello["fees"]["min_notional_quote_units"])
        if notional < min_notional:
            return []
        # Keep the estimated impact of the spend at or under the cap: on an
        # untouched pool the impact grows in proportion to the amount, so
        # one proportional cut lands on the cap.
        fee_bps = int(state.hello["fees"]["fee_bps"])
        spend = notional * (10_000 - fee_bps) // 10_000
        impact = state.impact_bps(self.symbol, spend)
        if impact is not None and impact > IMPACT_CAP_BPS:
            notional = notional * IMPACT_CAP_BPS // impact
            if notional < min_notional:
                return []
        order = {
            "side": "buy",
            "client_id": state.client_id(),
            "symbol": self.symbol,
            "notional_quote_units": notional,
            "max_slippage_bps": slippage_bps,
        }
        self.pending_id = order["client_id"]
        return [order]


def main():
    bot = Bot()
    makefun_bars.run(bot.on_batch)


if __name__ == "__main__":
    main()
