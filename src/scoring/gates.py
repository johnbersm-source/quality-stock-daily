"""
gates.py
weights.yaml의 2개 독립 게이트를 RawFinancials → GateResult로 변환한다.
실제 낙폭/DCF 계산 자체는 각 collector(stress_test_gate)와 dcf.py가 담당하고,
여기서는 그 결과를 최종 판정 로직(final_verdict_logic)이 쓸 수 있는 공통
GateResult 형태로 정리만 한다.
"""
from __future__ import annotations

from .models import GateResult, RawFinancials


def evaluate_stress_test_gate(data: RawFinancials) -> GateResult:
    r = data.stress_test_result
    if r is None:
        return GateResult(
            gate_key="stress_test_gate",
            passed=None,
            detail={},
            reason="스트레스테스트 데이터 없음(collector 미실행) — 판정 불가",
        )
    passed = r.get("passed")
    idx_dd = r.get("index_drawdown_pct")
    stk_dd = r.get("stock_drawdown_pct")
    reason = (
        f"구간 {r.get('window', '?')}: 지수 낙폭 {idx_dd}% vs 종목 낙폭 {stk_dd}% "
        f"→ {'통과(지수보다 덜 빠지거나 상승)' if passed else '미통과(지수보다 더 하락)'}"
    )
    return GateResult(gate_key="stress_test_gate", passed=bool(passed), detail=r, reason=reason)


def evaluate_margin_of_safety_gate(data: RawFinancials) -> GateResult:
    r = data.margin_of_safety_result
    if r is None:
        return GateResult(
            gate_key="margin_of_safety_gate",
            passed=None,
            detail={},
            reason="안전마진(DCF) 데이터 없음 — 판정 불가",
        )
    passed = r.get("passed")
    reason = r.get("reason", "")
    if not reason:
        base_mos = r.get("mos_base_pct")
        reason = f"Base 시나리오 MoS={base_mos}%" if base_mos is not None else "판정 근거 없음"
    return GateResult(gate_key="margin_of_safety_gate", passed=passed, detail=r, reason=reason)


def evaluate_all_gates(data: RawFinancials) -> list[GateResult]:
    return [evaluate_stress_test_gate(data), evaluate_margin_of_safety_gate(data)]
