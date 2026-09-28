"""
test_scorecard_samsung.py
스코어카드 엔진(src/scoring/)을 이 세션에서 이미 실측 검증한 삼성전자(005930)
데이터로 회귀 테스트한다.

주의: 이 테스트는 "실제 예시 하나로 검증" 원칙을 지키기 위해, 이 대화 세션
중 DART/KIS 실측으로 이미 확정된 수치만 사용한다. 이 스크립트를 작성하는
시점에 대화 컨텍스트에 남아있지 않은 값(예: 이익잉여금 12년 전체 시계열의
개별 연도 금액, 5년치 순이익 원계열)은 지어내지 않고 None으로 비워
두었다 — 해당 서브컴포넌트가 '데이터 없음'으로 정직하게 스킵되는지
확인하는 것도 이 테스트의 목적 중 하나다(결측과 저평가를 혼동하지
않는다는 모델 설계 원칙의 검증).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.scoring.dcf import compute_margin_of_safety
from src.scoring.models import QualitativeScore, RawFinancials
from src.scoring.scorecard import compute_score, format_report_text

# --- 실측 확인된 Owner Earnings 입력 (DART 전체재무제표, 이 세션 grep 결과) ---
OCF_FY2025 = 85_315_148  # 백만원
CAPEX_FY2025 = 47_522_179 + 4_630_970  # 유형+무형자산 취득, 백만원
OCF_FY2024 = 72_982_621
CAPEX_FY2024 = 51_406_355 + 2_335_284
OCF_FY2023 = 44_137_427
CAPEX_FY2023 = 57_611_292 + 2_922_875

fcf_latest = (OCF_FY2025 - CAPEX_FY2025) / 1e6  # 백만원 → 조원 (1조원 = 1,000,000백만원)
fcf_3y_avg = (
    (OCF_FY2025 - CAPEX_FY2025) + (OCF_FY2024 - CAPEX_FY2024) + (OCF_FY2023 - CAPEX_FY2023)
) / 3 / 1e6

mos_result = compute_margin_of_safety(
    fcf_latest=fcf_latest,
    fcf_3y_avg=fcf_3y_avg,
    wacc_pct=7.63,
    market_cap=1581.4,  # 조원, 이 세션에서 KIS API + 수기계산 이중검증한 값
)

margin_of_safety_result = {
    "mos_bear_pct": mos_result.scenarios[0].margin_of_safety_pct,
    "mos_base_pct": mos_result.scenarios[1].margin_of_safety_pct,
    "mos_bull_pct": mos_result.scenarios[2].margin_of_safety_pct,
    "passed": mos_result.passed,
    "reason": mos_result.reason,
}

# --- 실측 확인된 스트레스테스트 결과 (2026.6.22~7.30 반도체발 급락 구간) ---
stress_test_result = {
    "stock_ticker": "005930",
    "index_ticker": "1001",
    "window": "20260622~20260730",
    "index_drawdown_pct": -38.63,
    "stock_drawdown_pct": -42.90,
    "passed": False,  # 종목이 지수보다 더 크게 하락 → 미통과
}

data = RawFinancials(
    ticker="005930",
    company_name="삼성전자",
    market="KR",
    as_of_date="2026-09-28",

    # A블록: ROIC-WACC 스프레드만 실측 보유, ROE/해자는 이 스크립트 작성 시점
    # 컨텍스트에 정확한 5년 원계열이 없어 정직하게 None 처리
    roe_5y_pct=None,
    roic_pct=7.20,
    wacc_pct=7.63,
    wacc_assumptions_note="표준 가정: Rf=3.5%, Beta=1.0, ERP=5.5%, 세율=24%, 세전타인자본비용=4.0%",
    moat_qual=QualitativeScore(
        points_awarded=6.5, max_points=8,
        evidence=("메모리반도체(D램) 글로벌 점유율 1위(가격결정력 부분적 보유), "
                  "다만 파운드리는 후발주자로 경쟁열위 — 사업부별 해자 강도가 상이해 만점 대비 감점"),
    ),

    # B블록: 이 시점 컨텍스트에 5년 순이익 원계열/총자산 최신값이 없어 None —
    # OCF/NI 비교 서브컴포넌트는 정직하게 스킵되어야 함(테스트 검증 포인트)
    ocf_5y=[OCF_FY2023, OCF_FY2024, OCF_FY2025],
    ni_5y=None,
    total_assets_latest=None,
    ifrs18_segment_disclosed=True,
    ifrs18_mpm_qual=QualitativeScore(
        points_awarded=1.0, max_points=1.5,
        evidence="사업부문별(DX/DS/SDC/Harman) 분리공시는 확인되나 MPM(경영진성과지표) 공식화 여부는 미확인",
    ),

    # C블록: 12년 전체 시계열은 이 스크립트 시점에 컨텍스트 밖 — None 처리
    retained_earnings_series=None,
    debt_ratio_pct=None,
    altman_z=9.40,

    # D블록
    stress_test_result=stress_test_result,

    # E블록: 미보유
    revenue_cagr_3y_pct=None,
    op_income_cagr_3y_pct=None,

    # F블록: 미보유
    per=None,
    pbr=None,
    ev_ebit=None,

    # G블록: 미보유
    market_cap=1581.4,
    dividend_total=None,
    net_buyback_amount=None,
    net_debt_repayment=None,
    treasury_retirement_ratio_pct=None,

    # H블록
    macro_qual=QualitativeScore(
        points_awarded=3.0, max_points=8,
        evidence="2026년 반도체 업황 변동성 확대(감산/가격 사이클), AI향 HBM 수요 성장은 긍정적 재료지만 사이클 저점 미확인",
    ),

    margin_of_safety_result=margin_of_safety_result,
)

if __name__ == "__main__":
    report = compute_score(data)
    print(format_report_text(report))

    print("\n=== 회귀 검증 포인트 ===")
    assert report.gates[0].passed is False, "stress_test_gate는 미통과여야 함(실측: 지수보다 더 하락)"
    assert report.gates[1].passed is False, "margin_of_safety_gate는 미통과여야 함(실측: MoS 전부 음수)"
    assert report.final_grade == "관찰", "게이트 미통과 시 총점과 무관하게 '관찰' 등급이어야 함"

    ocf_block = next(c for b in report.blocks for c in b.components if c.name == "OCF/NI+발생액비율")
    assert ocf_block.points_awarded is None, "NI/총자산 데이터가 없으므로 이 컴포넌트는 None(채점불가)이어야 함"

    roe_block = next(c for b in report.blocks for c in b.components if c.name == "ROE")
    assert roe_block.points_awarded is None, "ROE 데이터가 없으므로 None(채점불가)이어야 함 — 0점 아님"

    print("PASS: 게이트 판정, 최종등급, 결측 컴포넌트 처리 전부 예상대로 동작함")
