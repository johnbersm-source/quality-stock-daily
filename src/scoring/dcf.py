"""
dcf.py
안전마진 게이트(margin_of_safety_gate, weights.yaml 정의)용 단순 Gordon Growth DCF.

방법론(weights.yaml gate_definitions.margin_of_safety_gate.method 그대로 구현):
- FCF(Owner Earnings) = OCF - CapEx(유형+무형자산 취득)
- 3개 시나리오: Bear(3개년 평균 FCF, g=1.5%), Base(최근 FCF, g=2.5%), Bull(최근 FCF, g=4.0%)
- V = FCF1 / (WACC - g),  FCF1 = FCF0 * (1+g)
- MoS = (내재가치 - 시가총액) / 내재가치, 통과조건 MoS >= 30%

이 세션에서 삼성전자 실측 데이터로 검증된 계산 로직(2026-09-28):
  FCF_bear=12.00조, g=1.5%, FCF_base/bull=33.16조, g=2.5%/4.0%, WACC=7.63%
  → 내재가치 Bear 198.7조 / Base 662.7조 / Bull 950.1조
  → 시가총액 1581.4조 대비 MoS: Bear -696.0% / Base -138.6% / Bull -66.4% (전부 미통과)
아래 compute_margin_of_safety()로 동일 입력을 넣으면 이 수치가 재현되는지
__main__에서 회귀 테스트로 확인한다.

주의(weights.yaml에 명시된 한계, 그대로 계승): 반도체 등 고변동 CapEx 업종은
단일단계 Gordon Growth 모델의 신뢰도가 낮다. 이 모듈은 3개 시나리오를 병기해
불확실성 범위를 드러내는 것으로 한계를 보완하되, 근본적으로 다단계 DCF가
아니라는 점은 리포트에 항상 명시해야 한다.
"""
from __future__ import annotations

from dataclasses import dataclass

WACC_ASSUMPTIONS_NOTE = (
    "표준 가정: 무위험금리(Rf)=3.5%, 베타=1.0, 시장위험프리미엄(ERP)=5.5%, "
    "법인세율=24%, 세전타인자본비용=4.0% — 모두 가정치이며 실측 업종 베타 등으로 "
    "고도화 가능"
)


@dataclass
class ScenarioResult:
    name: str
    fcf0: float
    growth_rate_pct: float
    fcf1: float
    intrinsic_value: float
    margin_of_safety_pct: float | None  # 시가총액 없으면 None


@dataclass
class MarginOfSafetyResult:
    wacc_pct: float
    market_cap: float | None
    scenarios: list[ScenarioResult]
    passed: bool | None  # 시나리오 전부(보수적으로 Base 기준) 판정, 시가총액 없으면 None
    reason: str


def compute_wacc(
    risk_free_rate_pct: float = 3.5,
    beta: float = 1.0,
    equity_risk_premium_pct: float = 5.5,
    pretax_cost_of_debt_pct: float = 4.0,
    tax_rate_pct: float = 24.0,
    equity_weight: float | None = None,
    debt_weight: float | None = None,
    market_cap: float | None = None,
    total_debt: float | None = None,
) -> float:
    """표준 가정 기반 WACC(%) 산출. equity_weight/debt_weight를 직접 주지 않으면
    market_cap과 total_debt로부터 계산한다(자기자본 비중은 실측)."""
    cost_of_equity = risk_free_rate_pct + beta * equity_risk_premium_pct
    after_tax_cost_of_debt = pretax_cost_of_debt_pct * (1 - tax_rate_pct / 100)

    if equity_weight is None or debt_weight is None:
        if market_cap is None or total_debt is None:
            raise ValueError(
                "equity_weight/debt_weight 또는 (market_cap, total_debt) 중 하나는 필요합니다."
            )
        total = market_cap + total_debt
        if total <= 0:
            raise ValueError("market_cap + total_debt가 0 이하입니다.")
        equity_weight = market_cap / total
        debt_weight = total_debt / total

    return equity_weight * cost_of_equity + debt_weight * after_tax_cost_of_debt


def _gordon_growth_value(fcf0: float, growth_rate_pct: float, wacc_pct: float) -> float:
    g = growth_rate_pct / 100
    wacc = wacc_pct / 100
    if wacc <= g:
        raise ValueError(
            f"WACC({wacc_pct}%)가 성장률({growth_rate_pct}%) 이하입니다 — "
            f"Gordon Growth 모델이 발산합니다(내재가치 무한대/음수)."
        )
    fcf1 = fcf0 * (1 + g)
    return fcf1 / (wacc - g)


def compute_margin_of_safety(
    fcf_latest: float,
    fcf_3y_avg: float,
    wacc_pct: float,
    market_cap: float | None,
    bear_growth_pct: float = 1.5,
    base_growth_pct: float = 2.5,
    bull_growth_pct: float = 4.0,
    pass_threshold_pct: float = 30.0,
) -> MarginOfSafetyResult:
    """weights.yaml margin_of_safety_gate 그대로 구현. fcf_latest/fcf_3y_avg/market_cap은
    전부 같은 통화·단위로 맞춰서 전달해야 한다(예: 전부 '조원' 또는 전부 '원').

    통과 판정은 Base 시나리오 기준으로 한다(Bear는 보수적 하한, Bull은 낙관적
    상한 참고용 — weights.yaml의 verified_example도 Base 기준으로 최종 '미통과'
    판정했음)."""
    scenarios_spec = [
        ("Bear", fcf_3y_avg, bear_growth_pct),
        ("Base", fcf_latest, base_growth_pct),
        ("Bull", fcf_latest, bull_growth_pct),
    ]

    scenarios: list[ScenarioResult] = []
    for name, fcf0, g in scenarios_spec:
        iv = _gordon_growth_value(fcf0, g, wacc_pct)
        fcf1 = fcf0 * (1 + g / 100)
        mos = None
        if market_cap is not None and iv:
            mos = (iv - market_cap) / iv * 100
        scenarios.append(ScenarioResult(
            name=name, fcf0=fcf0, growth_rate_pct=g, fcf1=fcf1,
            intrinsic_value=iv, margin_of_safety_pct=mos,
        ))

    base = next(s for s in scenarios if s.name == "Base")
    if market_cap is None:
        passed = None
        reason = "시가총액 데이터 없음 — 안전마진 판정 불가(데이터 확보 후 재계산 필요)"
    else:
        passed = base.margin_of_safety_pct is not None and base.margin_of_safety_pct >= pass_threshold_pct
        reason = (
            f"Base 시나리오 MoS={base.margin_of_safety_pct:.1f}% "
            f"({'>=' if passed else '<'} 통과기준 {pass_threshold_pct}%)"
        )

    return MarginOfSafetyResult(
        wacc_pct=wacc_pct, market_cap=market_cap, scenarios=scenarios,
        passed=passed, reason=reason,
    )


if __name__ == "__main__":
    print("=== 회귀 테스트: 삼성전자 실측 데이터(2026-09-28 세션 검증값) 재현 ===")
    result = compute_margin_of_safety(
        fcf_latest=33.16,       # 조원
        fcf_3y_avg=12.00,       # 조원
        wacc_pct=7.63,
        market_cap=1581.4,      # 조원
    )
    for s in result.scenarios:
        print(f"{s.name}: FCF0={s.fcf0}조, g={s.growth_rate_pct}%, "
              f"내재가치={s.intrinsic_value:.1f}조, MoS={s.margin_of_safety_pct:.1f}%")
    print(f"판정: {'통과' if result.passed else '미통과'} — {result.reason}")

    print("\n기대값(이 세션에서 수기 계산한 원 결과): "
          "Bear 198.7조/-696.0%, Base 662.7조/-138.6%, Bull 950.1조/-66.4%, 미통과")
