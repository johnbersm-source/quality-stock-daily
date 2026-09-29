"""
blocks.py
weights.yaml 8블록(100점) 각 컴포넌트의 실제 채점 함수.

채점 기준(밴드/컷라인)에 대한 원칙:
- PDF 원본에 이미 절대컷이 명시된 항목(ROE, PBR, 부채비율)은 PDF 기준선을
  그대로 준용하되 배점만 v3 비중으로 재조정한다(PDF와 다른 판정을 내리는
  게 아니라 같은 컷라인을 다른 배점으로 쓰는 것).
- PDF에 없는 v3 신설 지표(ROIC-WACC 스프레드, Altman Z, Sloan 발생액비율,
  Shareholder Yield, 소각비율 등)는 공개된 재무분석 표준 밴드를 준용하고,
  그 출처/기준을 각 함수 docstring에 명시한다. 절대 임의의 숫자를 직관으로
  만들지 않는다.
- 데이터가 없으면 0점이 아니라 None(채점불가)을 반환한다 — 결측과 저평가를
  절대 혼동하지 않는다(모델의 핵심 원칙, models.py ComponentResult 참고).
- 정성평가(경제적 해자, IFRS18 MPM, 거시·사이클)는 이 파일이 판단하지 않고
  RawFinancials에 이미 채워진 QualitativeScore(근거 필수)를 그대로 반영한다.
"""
from __future__ import annotations

from .models import BlockResult, ComponentResult, RawFinancials


def _lerp_score(value: float, low: float, high: float, low_pts: float, high_pts: float) -> float:
    """value가 low~high 구간에 있으면 선형보간, 범위 밖이면 각 경계값으로 클램프."""
    if high == low:
        return high_pts
    t = (value - low) / (high - low)
    t = max(0.0, min(1.0, t))
    return low_pts + t * (high_pts - low_pts)


# ------------------------------------------------------------
# A블록: 수익성·해자 (22점)
# ------------------------------------------------------------

def score_roe(data: RawFinancials, max_points: float = 8) -> ComponentResult:
    """PDF 원본 절대컷(5년평균 ROE 15%=만점대, 10%=관찰선) 준용, 8점 스케일로 재조정.
    업종 중앙값(industry_roe_median_pct)이 있으면 Z-score 대용으로 상대배수를 우선
    사용(더 엄밀한 v3 방법론), 없으면 절대컷으로 대체."""
    if not data.roe_5y_pct:
        return ComponentResult("ROE", None, max_points, "5년 ROE 데이터 없음 — 채점 불가")

    avg_roe = sum(data.roe_5y_pct) / len(data.roe_5y_pct)
    years_note = f"({len(data.roe_5y_pct)}개년 평균)"

    if data.industry_roe_median_pct:
        rel = avg_roe / data.industry_roe_median_pct if data.industry_roe_median_pct else None
        if rel is None:
            pts = _lerp_score(avg_roe, 10, 15, 0, max_points)
            note = f"업종중앙값 무효, 절대컷 대체: 평균ROE={avg_roe:.1f}% {years_note}"
        else:
            pts = _lerp_score(rel, 0.8, 1.3, 0, max_points)
            note = (f"업종 상대배수={rel:.2f}배(평균ROE={avg_roe:.1f}% vs "
                     f"업종중앙값={data.industry_roe_median_pct:.1f}%) {years_note}")
    else:
        pts = _lerp_score(avg_roe, 10, 15, 0, max_points)
        note = (f"업종 모집단 미확보 — PDF 절대컷(10%~15%) 대체 적용: "
                f"평균ROE={avg_roe:.1f}% {years_note}")
    return ComponentResult("ROE", round(pts, 2), max_points, note)


def score_moat(data: RawFinancials, max_points: float = 8) -> ComponentResult:
    """경제적 해자 — 순수 정성평가. RawFinancials.moat_qual(근거 필수, QualitativeScore)이
    미리 채워져 있어야 하며, 이 함수는 그 값을 그대로 반영할 뿐 스스로 판단하지 않는다."""
    if data.moat_qual is None:
        return ComponentResult("경제적 해자(정성)", None, max_points, "정성평가 미입력 — 채점 불가")
    q = data.moat_qual
    return ComponentResult("경제적 해자(정성)", q.points_awarded, max_points, q.evidence)


def score_roic_wacc_spread(data: RawFinancials, max_points: float = 6) -> ComponentResult:
    """ROIC-WACC 스프레드. 밴드: >=+3%p 만점, -2%p~+3%p 선형보간, <=-2%p 0점.
    (일반적 EVA/초과수익 분석에서 쓰이는 완충 구간 — 스프레드가 살짝 음수라도
    업종 변동성 내 '비슷함'으로 보는 통상적 해석을 반영, 이 세션에서 삼성전자
    스프레드 -0.43%p를 '비슷함' 밴드로 판정한 것과 일관됨)"""
    if data.roic_pct is None or data.wacc_pct is None:
        return ComponentResult("ROIC-WACC 스프레드", None, max_points, "ROIC 또는 WACC 데이터 없음")
    spread = data.roic_pct - data.wacc_pct
    pts = _lerp_score(spread, -2, 3, 0, max_points)
    assumption_note = f" [{data.wacc_assumptions_note}]" if data.wacc_assumptions_note else ""
    note = f"ROIC={data.roic_pct:.2f}%, WACC={data.wacc_pct:.2f}%, 스프레드={spread:+.2f}%p{assumption_note}"
    return ComponentResult("ROIC-WACC 스프레드", round(pts, 2), max_points, note)


def score_block_a(data: RawFinancials) -> BlockResult:
    return BlockResult("A_profitability_moat", "수익성·해자", 22, [
        score_roe(data),
        score_moat(data),
        score_roic_wacc_spread(data),
    ])


# ------------------------------------------------------------
# B블록: 이익의질·회계투명성 (9점)
# ------------------------------------------------------------

def score_ocf_quality(data: RawFinancials, max_points: float = 6) -> ComponentResult:
    """OCF>NI 연도비율(3점) + Sloan 발생액비율(3점).
    Sloan(1996) 원 연구 기준: 발생액비율=(NI-OCF)/총자산. 낮을수록(음수에 가까울수록)
    이익의 질이 높다고 알려진 실증 결과를 밴드로 사용: <=-5% 만점, -5%~+5% 선형,
    >=+5% 0점."""
    parts_pts = 0.0
    notes = []
    half = max_points / 2

    if data.ocf_5y and data.ni_5y and len(data.ocf_5y) == len(data.ni_5y) and data.ocf_5y:
        beat_years = sum(1 for o, n in zip(data.ocf_5y, data.ni_5y) if o > n)
        ratio = beat_years / len(data.ocf_5y)
        parts_pts += ratio * half
        notes.append(f"OCF>NI {beat_years}/{len(data.ocf_5y)}개년")
    else:
        notes.append("OCF/NI 시계열 없음 — 해당 부분 채점불가(0으로 취급하지 않고 배점 자체를 제외)")
        max_points -= half  # 이 서브파트는 배점에서 제외(결측=0점 혼동 방지)

    if data.ni_5y and data.ocf_5y and data.total_assets_latest:
        latest_ni, latest_ocf = data.ni_5y[-1], data.ocf_5y[-1]
        accruals_pct = (latest_ni - latest_ocf) / data.total_assets_latest * 100
        sub = _lerp_score(-accruals_pct, -5, 5, 0, half)  # 부호 반전: accruals가 낮을수록 고득점
        parts_pts += sub
        notes.append(f"Sloan 발생액비율={accruals_pct:+.2f}%")
    else:
        notes.append("발생액비율 계산용 총자산 데이터 없음 — 해당 부분 채점불가")
        max_points -= half

    if max_points <= 0:
        return ComponentResult("OCF/NI+발생액비율", None, 6, "; ".join(notes))
    return ComponentResult("OCF/NI+발생액비율", round(parts_pts, 2), max_points, "; ".join(notes))


def score_ifrs18(data: RawFinancials, max_points: float = 3) -> ComponentResult:
    """IFRS18 투명성 = 부문별 분리공시 여부(사실, 1.5점) + MPM 공식화 여부(정성, 1.5점)."""
    half = max_points / 2
    pts = 0.0
    notes = []
    awarded_any = False

    if data.ifrs18_segment_disclosed is not None:
        pts += half if data.ifrs18_segment_disclosed else 0
        notes.append(f"부문별 분리공시: {'있음' if data.ifrs18_segment_disclosed else '없음'}")
        awarded_any = True
    else:
        notes.append("부문별 분리공시 여부 미확인")

    if data.ifrs18_mpm_qual is not None:
        pts += data.ifrs18_mpm_qual.points_awarded
        notes.append(data.ifrs18_mpm_qual.evidence)
        awarded_any = True
    else:
        notes.append("MPM 공식화 정성평가 미입력")

    if not awarded_any:
        return ComponentResult("IFRS18 투명성", None, max_points, "; ".join(notes))
    return ComponentResult("IFRS18 투명성", round(pts, 2), max_points, "; ".join(notes))


def score_block_b(data: RawFinancials) -> BlockResult:
    return BlockResult("B_earnings_quality", "이익의질·회계투명성", 9, [
        score_ocf_quality(data),
        score_ifrs18(data),
    ])


# ------------------------------------------------------------
# C블록: 재무완충력·이익방어력 (14점)
# ------------------------------------------------------------

def score_retained_earnings(data: RawFinancials, max_points: float = 6) -> ComponentResult:
    """이익잉여금 장기추이. 만점 6점에서 시작해 감소연도 1회당 -1점(최소 0),
    전체 기간 CAGR이 음수면 추가로 0점 처리(장기 훼손 신호)."""
    series = data.retained_earnings_series
    if not series or len(series) < 2:
        return ComponentResult("이익잉여금 장기추이", None, max_points, "10년+ 시계열 데이터 없음")

    values = [v for _, v in series]
    decline_years = sum(1 for i in range(1, len(values)) if values[i] < values[i - 1])
    years_span = len(values) - 1
    cagr = (values[-1] / values[0]) ** (1 / years_span) - 1 if values[0] > 0 and years_span > 0 else None

    if cagr is not None and cagr < 0:
        pts = 0.0
    else:
        pts = max(0.0, max_points - decline_years)

    note = (f"{series[0][0]}~{series[-1][0]} ({len(series)}개년), 감소연도 {decline_years}회, "
            f"CAGR={cagr*100:.1f}%" if cagr is not None else
            f"{series[0][0]}~{series[-1][0]}, 감소연도 {decline_years}회, CAGR 계산불가(시작값<=0)")
    return ComponentResult("이익잉여금 장기추이", round(pts, 2), max_points, note)


def score_debt_and_z(data: RawFinancials, max_points: float = 8) -> ComponentResult:
    """부채비율(PDF 절대컷 준용, 4점) + Altman Z-Score(표준 임계값 1.81/2.99, 4점).
    주의: 시가총액이 매우 큰 기업은 Z-Score의 D요소(시총/부채)가 부풀려질 수 있어
    해석에 유의(이 세션 삼성전자 사례에서 실제로 관찰된 왜곡 가능성 — weights.yaml
    C블록 note 참고)."""
    half = max_points / 2
    pts = 0.0
    notes = []
    awarded_any = False

    if data.debt_ratio_pct is not None:
        # PDF 절대컷 준용: 부채비율<=100% 만점, 100~200% 선형, >=200% 0점
        sub = _lerp_score(-data.debt_ratio_pct, -200, -100, 0, half)
        pts += sub
        notes.append(f"부채비율={data.debt_ratio_pct:.1f}%")
        awarded_any = True
    else:
        notes.append("부채비율 데이터 없음")

    if data.altman_z is not None:
        sub = _lerp_score(data.altman_z, 1.81, 2.99, 0, half)
        pts += sub
        notes.append(f"Altman Z-Score={data.altman_z:.2f} (안전선 2.99, 위험선 1.81)")
        if data.altman_z > 8:
            notes.append("※시총 과대로 인한 Z-Score 왜곡 가능성 있음 — 참고용")
        awarded_any = True
    else:
        notes.append("Altman Z-Score 데이터 없음")

    if not awarded_any:
        return ComponentResult("부채비율+Altman Z", None, max_points, "; ".join(notes))
    return ComponentResult("부채비율+Altman Z", round(pts, 2), max_points, "; ".join(notes))


def score_block_c(data: RawFinancials) -> BlockResult:
    return BlockResult("C_balance_sheet_resilience", "재무완충력·이익방어력", 14, [
        score_retained_earnings(data),
        score_debt_and_z(data),
    ])


# ------------------------------------------------------------
# D블록: 주가방어력 (12점, PDF 없음 — 스트레스테스트 게이트 실측 기반)
# ------------------------------------------------------------

def score_drawdown_defense(data: RawFinancials, max_points: float = 12) -> ComponentResult:
    """게이트와 동일한 실측 데이터를 연속점수로 환산: 지수 대비 초과방어폭
    (종목낙폭-지수낙폭, 양수=덜 빠짐)이 클수록 고득점. 게이트 자체가 미통과면
    (초과방어폭<0) 이 컴포넌트도 0점으로 일관 처리한다."""
    r = data.stress_test_result
    if r is None:
        return ComponentResult("주가방어력(실측 스트레스테스트)", None, max_points, "스트레스테스트 데이터 없음")
    outperf = r.get("stock_drawdown_pct", 0) - r.get("index_drawdown_pct", 0)
    pts = _lerp_score(outperf, 0, 10, 0, max_points)
    note = (f"구간 {r.get('window')}: 초과방어폭={outperf:+.2f}%p "
            f"(종목{r.get('stock_drawdown_pct')}% vs 지수{r.get('index_drawdown_pct')}%)")
    return ComponentResult("주가방어력(실측 스트레스테스트)", round(pts, 2), max_points, note)


def score_block_d(data: RawFinancials) -> BlockResult:
    return BlockResult("D_drawdown_defense", "주가방어력 (신설, PDF 없음)", 12, [
        score_drawdown_defense(data),
    ])


# ------------------------------------------------------------
# E블록: 추세·성장 (7점, PDF 없음)
# ------------------------------------------------------------

def score_growth(data: RawFinancials, max_points: float = 7) -> ComponentResult:
    """매출 3년 CAGR과 영업이익 3년 CAGR의 평균. 밴드: >=10% 만점, 0~10% 선형, <0 0점."""
    vals = [v for v in [data.revenue_cagr_3y_pct, data.op_income_cagr_3y_pct] if v is not None]
    if not vals:
        return ComponentResult("추세·성장(3년 CAGR)", None, max_points, "매출/영업이익 CAGR 데이터 없음")
    avg = sum(vals) / len(vals)
    pts = _lerp_score(avg, 0, 10, 0, max_points)
    note = f"매출CAGR={data.revenue_cagr_3y_pct}, 영업이익CAGR={data.op_income_cagr_3y_pct} (평균 {avg:.1f}%)"
    return ComponentResult("추세·성장(3년 CAGR)", round(pts, 2), max_points, note)


def score_block_e(data: RawFinancials) -> BlockResult:
    return BlockResult("E_trend_growth", "추세·성장", 7, [score_growth(data)])


# ------------------------------------------------------------
# F블록: 가격 (18점)
# ------------------------------------------------------------

def score_price_multiples(data: RawFinancials, max_points: float = 18) -> ComponentResult:
    """PER·PBR·EV/EBIT 3개 배수 각 균등 배점. 업종중앙값 있으면 상대배수, 없으면
    PDF 절대컷(PBR<=1 만점) 준용 확장 절대컷 사용:
    PER: <=10 만점, 10~20 선형, >=20 0점 / PBR: <=1 만점, 1~2 선형, >=2 0점 /
    EV·EBIT: <=8 만점, 8~15 선형, >=15 0점
    (절대 안전마진은 별도 게이트에서 처리 — 여기는 상대가치 배수만 담당)"""
    each = max_points / 3
    parts = []
    total_pts = 0.0
    total_possible = 0.0

    if data.per is not None:
        pts = _lerp_score(-data.per, -20, -10, 0, each)
        parts.append(f"PER={data.per:.2f}")
        total_pts += pts
        total_possible += each
    if data.pbr is not None:
        pts = _lerp_score(-data.pbr, -2, -1, 0, each)
        parts.append(f"PBR={data.pbr:.2f}")
        total_pts += pts
        total_possible += each
    if data.ev_ebit is not None:
        pts = _lerp_score(-data.ev_ebit, -15, -8, 0, each)
        parts.append(f"EV/EBIT={data.ev_ebit:.2f}")
        total_pts += pts
        total_possible += each

    if not parts:
        return ComponentResult("PER·PBR·EV/EBIT 복수배수", None, max_points, "가격배수 데이터 없음")

    rel_note = ""
    if data.industry_per_median or data.industry_pbr_median:
        rel_note = (f" [업종중앙값 PER={data.industry_per_median}, "
                     f"PBR={data.industry_pbr_median} — 참고용, 절대컷 병기]")
    note = ", ".join(parts) + rel_note
    return ComponentResult("PER·PBR·EV/EBIT 복수배수", round(total_pts, 2), round(total_possible, 2), note)


def score_block_f(data: RawFinancials) -> BlockResult:
    return BlockResult("F_price", "가격", 18, [score_price_multiples(data)])


# ------------------------------------------------------------
# G블록: 주주환원·지배구조 (10점)
# ------------------------------------------------------------

def score_shareholder_yield(data: RawFinancials, max_points: float = 4) -> ComponentResult:
    """(배당+순매입+순부채상환)/시가총액. 밴드: >=5% 만점, 0~5% 선형, <0 0점.

    2026-09-30: 상법개정 수혜(업종) 컴포넌트 신설로 G블록 내 배점을
    5→4로 재조정(4+3+3=10, 총점 100점 불변 — README '남은 작업' 참고)."""
    if data.market_cap is None or not data.market_cap:
        return ComponentResult("Shareholder Yield", None, max_points, "시가총액 데이터 없음")
    components = [data.dividend_total, data.net_buyback_amount, data.net_debt_repayment]
    if all(c is None for c in components):
        return ComponentResult("Shareholder Yield", None, max_points, "배당/자사주/부채상환 데이터 없음")
    total = sum(c for c in components if c is not None)
    yield_pct = total / data.market_cap * 100
    pts = _lerp_score(yield_pct, 0, 5, 0, max_points)
    note = f"Shareholder Yield={yield_pct:.2f}% (배당+순매입+순부채상환 합계 기준)"
    return ComponentResult("Shareholder Yield", round(pts, 2), max_points, note)


def score_treasury_retirement(data: RawFinancials, max_points: float = 3) -> ComponentResult:
    """발행주식 대비 자사주 소각비율(%). 밴드: >=3% 만점, 0~3% 선형, 0 → 0점.

    2026-09-30: 컴포넌트명에서 "(상법개정 수혜)"를 뗐다 — 이제 상법개정 수혜는
    아래 score_commercial_code_benefit()이 별도로 담당한다(이전엔 이 소각비율
    컴포넌트 하나가 원본 PDF 항목 ⑤ 주주환원과 ⑩ 상법개정 수혜 두 개를 동시에
    대신하고 있었음 — PDF vs v3 비교 부록에서 발견한 갭, README 참고). 배점도
    5→3으로 재조정."""
    if data.treasury_retirement_ratio_pct is None:
        return ComponentResult("소각비율", None, max_points, "소각비율 데이터 없음")
    pts = _lerp_score(data.treasury_retirement_ratio_pct, 0, 3, 0, max_points)
    note = f"발행주식 대비 소각비율={data.treasury_retirement_ratio_pct:.2f}%"
    return ComponentResult("소각비율", round(pts, 2), max_points, note)


def score_commercial_code_benefit(data: RawFinancials, max_points: float = 3) -> ComponentResult:
    """상법개정 수혜(업종) — PDF 원본 항목 ⑩. 지주·금융·자산주 등 상법개정
    (이사 충실의무 확대, 집중투표제 등 지배구조 규제 강화)의 직접 수혜
    업종인지, 그리고 그에 선제 대응(지배구조 개편·주주환원 확대 공시 등)했는지
    Gemini 정성평가(qualitative.py)로 채점한다. 근거 없는 점수는
    QualitativeScore.__post_init__에서 자체 거부되므로 이중 안전장치."""
    q = data.commercial_code_benefit_qual
    if q is None:
        return ComponentResult("상법개정 수혜(업종, 정성)", None, max_points, "정성평가 미입력 — 채점 불가")
    return ComponentResult("상법개정 수혜(업종, 정성)", q.points_awarded, max_points, q.evidence)


def score_block_g(data: RawFinancials) -> BlockResult:
    return BlockResult("G_shareholder_return_governance", "주주환원·지배구조", 10, [
        score_shareholder_yield(data),
        score_treasury_retirement(data),
        score_commercial_code_benefit(data),
    ])


# ------------------------------------------------------------
# H블록: 거시·사이클·재료 (8점, PDF 없음, 전량 정성)
# ------------------------------------------------------------

def score_macro(data: RawFinancials, max_points: float = 8) -> ComponentResult:
    if data.macro_qual is None:
        return ComponentResult("거시·사이클·재료(정성)", None, max_points, "정성평가 미입력 — 채점 불가")
    q = data.macro_qual
    return ComponentResult("거시·사이클·재료(정성)", q.points_awarded, max_points, q.evidence)


def score_block_h(data: RawFinancials) -> BlockResult:
    return BlockResult("H_macro_catalyst", "거시·사이클·재료 (신설, PDF 없음)", 8, [score_macro(data)])


ALL_BLOCK_SCORERS = [
    score_block_a, score_block_b, score_block_c, score_block_d,
    score_block_e, score_block_f, score_block_g, score_block_h,
]
