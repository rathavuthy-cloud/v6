"""
sizing.py
==========
Risk-based position sizing for XAU/USD, using a real contract spec rather
than a hand-waved "pip value".

The model (this is just arithmetic, and it's worth understanding rather
than trusting):

    1 standard lot of XAUUSD = 100 troy oz  (broker-dependent; configurable)
    risk on a trade          = lots x contract_size x stop_distance_in_USD
    lots                     = (equity x risk_pct/100) / (contract_size x stop_distance)

Then the result is floored to the broker's lot step, because you can't
trade 0.037 lots if the step is 0.01.

The important function here is the guard: if the *smallest tradeable size*
still risks more than your risk budget, there is no valid size and the
correct answer is "don't take this trade", not "round up to 0.01 and hope".
Rounding up is how a $10 account takes a 185% risk on one gold trade. The
sizer refuses, explains the arithmetic, and lists the real options.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Defaults match a standard retail XAUUSD contract.
DEFAULT_CONTRACT_SIZE = 100.0   # troy oz per 1.00 lot
DEFAULT_LOT_STEP = 0.01
DEFAULT_MIN_LOT = 0.01
DEFAULT_MAX_LOT = 100.0


@dataclass
class SizingResult:
    ok: bool
    lots: Optional[float] = None
    risk_amount: Optional[float] = None          # intended risk in account currency
    actual_risk: Optional[float] = None          # risk at the rounded lot size
    actual_risk_pct: Optional[float] = None      # as % of equity
    min_lot_risk: Optional[float] = None         # risk at the smallest tradeable size
    min_lot_risk_pct: Optional[float] = None
    reason: str = ""
    warnings: list = field(default_factory=list)


def size_position(
    equity: Optional[float],
    risk_pct: Optional[float],
    stop_distance_usd: Optional[float],
    contract_size: float = DEFAULT_CONTRACT_SIZE,
    lot_step: float = DEFAULT_LOT_STEP,
    min_lot: float = DEFAULT_MIN_LOT,
    max_lot: float = DEFAULT_MAX_LOT,
) -> SizingResult:
    if not equity or equity <= 0:
        return SizingResult(ok=False, reason="Account equity not set \u2014 use /settings equity=... to enable sizing.")
    if not risk_pct or risk_pct <= 0:
        return SizingResult(ok=False, reason="Risk % not set \u2014 use /settings risk=1 to enable sizing.")
    if not stop_distance_usd or stop_distance_usd <= 0:
        return SizingResult(ok=False, reason="Stop distance unavailable \u2014 cannot size this trade.")

    risk_amount = equity * (risk_pct / 100.0)
    risk_per_lot = contract_size * stop_distance_usd          # $ risked per 1.00 lot
    raw_lots = risk_amount / risk_per_lot

    min_lot_risk = min_lot * risk_per_lot
    min_lot_risk_pct = min_lot_risk / equity * 100.0

    # Floor to the broker's lot step.
    steps = int(raw_lots / lot_step)
    lots = round(steps * lot_step, 10)

    if lots < min_lot:
        return SizingResult(
            ok=False,
            risk_amount=risk_amount,
            min_lot_risk=min_lot_risk,
            min_lot_risk_pct=min_lot_risk_pct,
            reason=(
                f"Too small to size safely at ${equity:,.2f} balance / {risk_pct:.1f}% risk and a "
                f"{stop_distance_usd:.2f} stop \u2014 even the smallest tradeable size ({min_lot:g} lot) "
                f"would risk ${min_lot_risk:,.2f} ({min_lot_risk_pct:.1f}% of your account)."
            ),
        )

    warnings = []
    if lots > max_lot:
        lots = max_lot
        warnings.append(f"Size capped at the {max_lot:g}-lot maximum \u2014 actual risk is below your target.")

    actual_risk = lots * risk_per_lot
    actual_risk_pct = actual_risk / equity * 100.0

    if actual_risk_pct > risk_pct * 1.5:
        warnings.append(
            f"Rounding to the {lot_step:g} lot step pushes real risk to {actual_risk_pct:.2f}% "
            f"(target {risk_pct:.1f}%). Consider a smaller contract size."
        )

    return SizingResult(
        ok=True,
        lots=lots,
        risk_amount=risk_amount,
        actual_risk=actual_risk,
        actual_risk_pct=actual_risk_pct,
        min_lot_risk=min_lot_risk,
        min_lot_risk_pct=min_lot_risk_pct,
        reason="",
        warnings=warnings,
    )


def too_small_advice() -> str:
    return (
        "Options: use <code>/contractsize</code> if your broker offers smaller XAUUSD contracts "
        "(micro gold is often 10 oz), trade a smaller instrument, or grow the account before "
        "sizing into gold at standard lots."
    )
