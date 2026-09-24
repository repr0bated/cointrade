"""Local task routing; model selection never replaces deterministic risk rules."""
from dataclasses import asdict, dataclass

NO_LLM = {'price_changes', 'liquidity', 'holder_concentration', 'authorities',
          'transaction_history', 'wallet_pnl', 'position_sizing', 'risk_rules'}
AUTO = {'unusual_activity', 'wallet_summary', 'conflicting_signals', 'pattern_analysis'}
ASTRA = {'debugging', 'algorithm_design', 'failure_analysis', 'code_improvement'}
TASKS = sorted(NO_LLM | AUTO | ASTRA)


@dataclass(frozen=True)
class Route:
    task: str
    model: str | None
    tier: str | None = None
    prompt_price: float = 0
    completion_price: float = 0
    max_input_bytes: int = 0
    max_output_tokens: int = 0
    reserved_cents: int = 0

    def describe(self):
        return asdict(self)


def select(task, tier=None):
    if task not in TASKS:
        raise ValueError(f'Unknown AI task: {task}')
    if task in NO_LLM:
        if tier is not None:
            raise ValueError('Deterministic tasks do not accept an AI cost tier')
        return Route(task, None)
    if task in ASTRA:
        if tier is not None:
            raise ValueError('Astra tasks select Astra explicitly, without an Auto Router tier')
        return Route(task, 'openai/gpt-6-astra', None, 10, 50, 2500, 1024, 10)
    tier = tier or ('medium' if task == 'pattern_analysis' else 'low')
    # xhigh and max exist at OpenRouter, but are outside this experiment's policy.
    limits = {'low': (1, 2, 6000, 512, 2), 'medium': (3, 12, 4000, 1024, 4),
              'high': (10, 50, 2500, 1024, 10)}
    if tier not in limits:
        raise ValueError('Allowed Auto Router tiers: low, medium, high')
    return Route(task, 'openrouter/auto', tier, *limits[tier])
