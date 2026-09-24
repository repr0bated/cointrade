from dataclasses import asdict, dataclass
import json
import math


@dataclass(frozen=True)
class Config:
    bankroll: float = 40.0
    reserve: float = 10.0
    position_size: float = 2.0
    max_exposure: float = 30.0
    halt_equity: float = 20.0
    max_positions: int = 5
    fee_bps: float = 30.0
    slippage_bps: float = 100.0
    network_fee: float = 0.005
    stop_loss: float = 0.15
    take_profit: float = 0.25
    max_hold_seconds: int = 3600
    max_snapshot_age: int = 120
    cooldown_seconds: int = 3600
    min_liquidity: float = 25000.0
    min_volume_h24: float = 10000.0
    min_age_seconds: int = 3600
    max_top10_share: float = 0.5
    min_score: float = 65.0

    def __post_init__(self):
        for name, value in asdict(self).items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite nonnegative number")
        for name in ("max_positions", "max_hold_seconds", "max_snapshot_age", "cooldown_seconds", "min_age_seconds"):
            if not isinstance(getattr(self, name), int) or getattr(self, name) < 1:
                raise ValueError(f"{name} must be a positive integer")
        if not 0 < self.halt_equity < self.bankroll:
            raise ValueError("halt_equity must be between zero and bankroll")
        if not 0 <= self.reserve < self.bankroll or not 0 < self.position_size <= self.max_exposure <= self.bankroll - self.reserve:
            raise ValueError("position_size <= max_exposure <= bankroll - reserve required")
        if not 0 < self.stop_loss < 1 or not 0 < self.take_profit:
            raise ValueError("invalid stop_loss or take_profit")
        if not 0 < self.max_top10_share <= 1 or self.min_score > 100:
            raise ValueError("invalid concentration or score threshold")
        if self.fee_bps >= 10000 or self.slippage_bps >= 10000:
            raise ValueError("fee and slippage must be less than 10000 bps")

    @classmethod
    def load(cls, path=None):
        if path is None:
            return cls()
        with open(path) as f:
            return cls(**json.load(f))
