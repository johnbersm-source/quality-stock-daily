"""
altman.py
Altman Z-Score(Altman, 1968 원 공식) 계산 — blocks.py의 score_debt_and_z()가
사용한다(부채비율과 함께 C블록 절반을 구성).

Z = 1.2*(운전자본/총자산) + 1.4*(이익잉여금/총자산) + 3.3*(EBIT/총자산)
    + 0.6*(시가총액/총부채) + 1.0*(매출액/총자산)
임계값(원 공식 그대로, blocks.py에서 이미 사용 중): Z>=2.99 안전, Z<1.81 위험.

2026-09-30 신설 배경: 이전까지 altman_z는 어떤 collector도 채워주지 않아
모든 종목에서 100% 구조적으로 N/A였다(C블록 8점 중 4점이 항상 결측 —
"미달" 등급이 자주 나오는 원인 중 하나로 리오님이 지적). 필요한 입력값
(유동자산·유동부채·이익잉여금·EBIT·시가총액·총부채·매출액)은 모두 기존
collector 호출로 이미 확보 가능한 값이라 새 API 연동 없이 계산만 추가한다.

필요 입력 중 하나라도 없으면(특히 유동자산/유동부채가 자주 결측) 0으로
채우지 않고 None을 반환한다 — 없는 데이터로 부실위험 점수를 지어내지 않는다
(사용자 표준 원칙).
"""
from __future__ import annotations


def compute_altman_z(
    current_assets: float | None,
    current_liabilities: float | None,
    retained_earnings: float | None,
    ebit: float | None,
    total_assets: float | None,
    market_cap: float | None,
    total_liabilities: float | None,
    revenue: float | None,
) -> float | None:
    required = [current_assets, current_liabilities, retained_earnings, ebit,
                total_assets, market_cap, total_liabilities, revenue]
    if any(v is None for v in required):
        return None
    if not total_assets or not total_liabilities:
        return None
    working_capital = current_assets - current_liabilities
    z = (
        1.2 * (working_capital / total_assets)
        + 1.4 * (retained_earnings / total_assets)
        + 3.3 * (ebit / total_assets)
        + 0.6 * (market_cap / total_liabilities)
        + 1.0 * (revenue / total_assets)
    )
    return round(z, 2)
