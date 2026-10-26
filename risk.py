"""Position sizing and account-level kill switches."""
import math
from dataclasses import dataclass

# Delta India charges 18% GST on trading fees.
TAKER_FEE = 0.0005 * 1.18
SLIPPAGE = 0.0002


@dataclass
class RiskConfig:
    risk_per_trade: float = 0.015   # fraction of equity lost if stop is hit
    max_leverage: float = 10.0      # hard cap on notional / equity (user limit)
    max_single_risk: float = 0.03   # skip trade if 1 contract alone risks more than this
    daily_loss_limit: float = 0.05  # stop trading for the UTC day after -5%
    max_drawdown: float = 0.25      # stop the bot entirely after -25% from peak


def size_contracts(equity, price, stop_price, contract_value, cfg: RiskConfig):
    """Return contracts to trade (0 = skip)."""
    stop_dist = abs(price - stop_price)
    if stop_dist <= 0 or equity <= 0:
        return 0
    # cost of hitting the stop per contract, incl. round-trip fees + slippage
    per_contract_loss = (stop_dist + price * 2 * (TAKER_FEE + SLIPPAGE)) * contract_value
    by_risk = math.floor(equity * cfg.risk_per_trade / per_contract_loss)
    by_leverage = math.floor(equity * cfg.max_leverage / (price * contract_value))
    qty = min(by_risk, by_leverage)
    if qty < 1:
        # allow a single contract only if it stays within max_single_risk and leverage cap
        if per_contract_loss <= equity * cfg.max_single_risk and by_leverage >= 1:
            return 1
        return 0
    return qty


class Guard:
    """Tracks equity and blocks new entries when limits are breached."""

    def __init__(self, cfg: RiskConfig, equity):
        self.cfg = cfg
        self.peak = equity
        self.day = None
        self.day_start = equity
        self.halted = False

    def update(self, equity, day):
        if day != self.day:
            self.day, self.day_start = day, equity
        self.peak = max(self.peak, equity)
        if equity <= self.peak * (1 - self.cfg.max_drawdown):
            self.halted = True

    def can_enter(self, equity):
        if self.halted:
            return False
        return equity > self.day_start * (1 - self.cfg.daily_loss_limit)
