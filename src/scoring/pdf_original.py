"""
pdf_original.py
PDF 원본(실전경제학 리서치 Vol.1) p.36 "10대 평가 항목 채점 기준표"를 그대로
구현한 병행 스코어카드. v3(weights.yaml, blocks.py)와는 완전히 독립적으로,
같은 RawFinancials 데이터를 원본 그대로의 3단계 컷라인으로 채점한다
(2026-09-30, 리오님 요청: "총점은 기존에 제시한 pdf로 매긴 점수 기준대로
매겨서 총점, 최종 등급을 v3와 함께 각각 결과를 내").

이 파일의 모든 컷라인은 이 세션에서 서브에이전트가 첨부된 Ver8 PDF 39페이지
전체를 실제로 읽어 p.36 표에서 그대로 추출한 값이다(추측/반올림/창작 없음).
원문 표(발췌):
  ①ROE: >=15%→10 / >=10%→6 / <10%→2
  ②영업현금흐름: 5년연속 OCF>NI→10 / 3년이상→6 / 불일치다수→2
  ③이익잉여금: 10년연속증가→10 / 5년증가→6 / 감소·정체→2
  ④경제적해자: 강력→10 / 약함→5 / 없음→1
  ⑤주주환원: 배당+소각 적극→10 / 배당만→6 / 미흡→2
  ⑥PBR수준: <1.0→10 / 1.0~1.5→6 / 1.5초과→3
  ⑦부채비율: <100%→10 / 100~200%→6 / 200%초과→2(0점 아님)
  ⑧자본배분능력: ROIC>WACC 5년이상→10 / 비슷→5 / ROIC<WACC→1
  ⑨IFRS18투명성: 분리명확+MPM적극→10 / 보통→5 / 미흡→2
  ⑩상법개정수혜: 직접수혜+선제대응→10 / 간접수혜→5 / 해당없음→2
등급: 90+ 최상급우량주 / 80+ 우량주 / 70+ 관찰대상 (PDF p.37, 이 3개 등급만
명시됨 — 70점 미만 등급명은 원문에 없음, 아래 PdfOriginalReport.final_grade
참고). 게이트(스트레스테스트/안전마진) 개념도 PDF 원문엔 없음(v3 고유 확장).

데이터가 없으면 v3와 동일한 원칙으로 0점이 아니라 None(채점불가)을 반환한다.
정성 항목(해자·IFRS18·상법개정)은 이 파일이 새로 조사하지 않고, v3가 이미
Gemini로 채점해둔 QualitativeScore를 그대로 재사용해 PDF의 3단계 배점으로
재매핑한다(근거 텍스트도 동일하게 인용 — 별도 Gemini 재호출 없음, API 비용
절감).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import ComponentResult, RawFinancials


def score_roe(data: RawFinancials, max_points: float = 10) -> ComponentResult:
    if not data.roe_5y_pct:
        return ComponentResult("① ROE", None, max_points, "5년 ROE 데이터 없음 — 채점 불가")
    avg = sum(data.roe_5y_pct) / len(data.roe_5y_pct)
    if avg >= 15:
        pts = 10
    elif avg >= 10:
        pts = 6
    else:
        pts = 2
    note = f"{len(data.roe_5y_pct)}개년 평균 ROE={avg:.1f}%"
    return ComponentResult("① ROE", pts, max_points, note)


def score_ocf(data: RawFinancials, max_points: float = 10) -> ComponentResult:
    """확보 연수가 5년 미만이면 "5년 연속"을 문자 그대로 판정할 수 없으므로,
    확보된 전체 구간에서 OCF>NI이고 그 연수가 5 이상일 때만 10점을 주며,
    그 외엔 "3년 이상 충족" 여부로 6점/2점을 가른다(확보 연수 부족을 note에
    정직하게 표기 — 임의로 10점을 주지 않는다)."""
    if not data.ocf_5y or not data.ni_5y:
        return ComponentResult("② 영업현금흐름", None, max_points, "OCF/NI 데이터 없음")
    pairs = list(zip(data.ocf_5y, data.ni_5y))
    beat = sum(1 for o, n in pairs if o > n)
    years = len(pairs)
    if beat == years and years >= 5:
        pts = 10
    elif beat >= 3:
        pts = 6
    else:
        pts = 2
    note = f"OCF>NI {beat}/{years}개년(확보 연수 {years}년)"
    return ComponentResult("② 영업현금흐름", pts, max_points, note)


def score_retained_earnings(data: RawFinancials, max_points: float = 10) -> ComponentResult:
    """"N년 연속 증가"를 확보한 시계열 전체 구간에서 감소연도 0회로 해석한다.
    10년 이상 확보+전구간 증가만 10점, 그보다 짧아도 전구간 증가면 6점
    (정체·감소가 없다는 사실 자체는 확인됐으나 "10년"을 주장할 수 없으므로),
    감소구간이 하나라도 있으면 2점."""
    series = data.retained_earnings_series
    if not series or len(series) < 2:
        return ComponentResult("③ 이익잉여금", None, max_points, "다개년 시계열 데이터 없음")
    values = [v for _, v in series]
    span = len(values) - 1
    decline = sum(1 for i in range(1, len(values)) if values[i] < values[i - 1])
    if decline == 0 and span >= 10:
        pts = 10
    elif decline == 0:
        pts = 6
    else:
        pts = 2
    note = f"{series[0][0]}~{series[-1][0]}({span}개 구간), 감소구간 {decline}회"
    return ComponentResult("③ 이익잉여금", pts, max_points, note)


def score_moat(data: RawFinancials, max_points: float = 10) -> ComponentResult:
    """v3가 이미 채점한 moat_qual(0~8점 스케일 정성평가)을 PDF의 3단계 배점
    (10/5/1)으로 재매핑한다(별도 Gemini 재호출 없음, 근거 텍스트 동일 인용).
    매핑 기준: v3 만점 대비 75%이상→강력(10), 25~75%→약함(5), 25%미만→없음(1)."""
    q = data.moat_qual
    if q is None:
        return ComponentResult("④ 경제적 해자", None, max_points, "정성평가 미입력 — 채점 불가")
    ratio = q.points_awarded / q.max_points if q.max_points else 0
    pts = 10 if ratio >= 0.75 else (5 if ratio >= 0.25 else 1)
    return ComponentResult("④ 경제적 해자", pts, max_points, q.evidence)


def score_shareholder_return(data: RawFinancials, max_points: float = 10) -> ComponentResult:
    """"적극 시행"의 정량 문턱은 PDF 원문에 숫자가 없어(서브에이전트 확인),
    이미 검증된 v3 Shareholder Yield 밴드(5% 이상=만점)를 그대로 준용해
    "배당+자사주 합계수익률 5%이상 & 배당·자사주 둘 다 있음=적극"으로
    판정한다(원문에 없는 숫자를 창작하지 않고 기존 검증된 밴드를 재사용)."""
    if data.market_cap is None:
        return ComponentResult("⑤ 주주환원", None, max_points, "시가총액 데이터 없음")
    if data.dividend_total is None and data.net_buyback_amount is None:
        return ComponentResult("⑤ 주주환원", None, max_points, "배당/자사주 데이터 없음")
    div = data.dividend_total or 0
    buyback = data.net_buyback_amount or 0
    total_yield_pct = (div + buyback) / data.market_cap * 100
    has_dividend = div > 0
    has_buyback = buyback > 0
    if total_yield_pct >= 5 and has_dividend and has_buyback:
        pts = 10
    elif has_dividend:
        pts = 6
    else:
        pts = 2
    note = (f"배당+자사주 합계수익률={total_yield_pct:.2f}%, "
            f"배당{'있음' if has_dividend else '없음'}/자사주{'있음' if has_buyback else '없음'}")
    return ComponentResult("⑤ 주주환원", pts, max_points, note)


def score_pbr(data: RawFinancials, max_points: float = 10) -> ComponentResult:
    if data.pbr is None:
        return ComponentResult("⑥ PBR 수준", None, max_points, "PBR 데이터 없음")
    pts = 10 if data.pbr < 1.0 else (6 if data.pbr <= 1.5 else 3)
    return ComponentResult("⑥ PBR 수준", pts, max_points, f"PBR={data.pbr:.2f}")


def score_debt_ratio(data: RawFinancials, max_points: float = 10) -> ComponentResult:
    """PDF 원문: 200% 초과는 2점(0점이 아님) — 서브에이전트가 39페이지 전체
    재확인, v3(0점 하한)와 다른 점이니 혼동 주의."""
    if data.debt_ratio_pct is None:
        return ComponentResult("⑦ 부채비율", None, max_points, "부채비율 데이터 없음")
    pts = 10 if data.debt_ratio_pct < 100 else (6 if data.debt_ratio_pct <= 200 else 2)
    return ComponentResult("⑦ 부채비율", pts, max_points, f"부채비율={data.debt_ratio_pct:.1f}%")


def score_capital_allocation(data: RawFinancials, max_points: float = 10) -> ComponentResult:
    """"5년 이상 지속"은 단일 연도 스냅샷으로 알 수 없어(v3와 동일한 데이터
    한계) 최신 연도 스프레드로 판정하되 "비슷함" 구간은 v3와 동일하게
    -2%p~+3%p로 정의한다(blocks.py score_roic_wacc_spread와 동일 밴드 재사용)."""
    if data.roic_pct is None or data.wacc_pct is None:
        return ComponentResult("⑧ 자본배분 능력", None, max_points, "ROIC 또는 WACC 데이터 없음")
    spread = data.roic_pct - data.wacc_pct
    pts = 10 if spread >= 3 else (5 if spread >= -2 else 1)
    note = (f"ROIC={data.roic_pct:.2f}%, WACC={data.wacc_pct:.2f}%, 스프레드={spread:+.2f}%p"
            f"(단일연도 스냅샷 — '5년 지속' 여부 자체는 판정 불가)")
    return ComponentResult("⑧ 자본배분 능력", pts, max_points, note)


def score_ifrs18(data: RawFinancials, max_points: float = 10) -> ComponentResult:
    if data.ifrs18_mpm_qual is None and data.ifrs18_segment_disclosed is None:
        return ComponentResult("⑨ IFRS18 투명성", None, max_points, "정성평가 미입력 — 채점 불가")
    q = data.ifrs18_mpm_qual
    ratio = (q.points_awarded / q.max_points) if (q is not None and q.max_points) else None
    if ratio is not None and ratio >= 0.75 and data.ifrs18_segment_disclosed:
        pts = 10
    elif data.ifrs18_segment_disclosed or (ratio is not None and ratio >= 0.25):
        pts = 5
    else:
        pts = 2
    parts = []
    if data.ifrs18_segment_disclosed is not None:
        parts.append(f"부문별 분리공시: {'있음' if data.ifrs18_segment_disclosed else '없음'}")
    parts.append(q.evidence if q is not None else "MPM 공식화 정성평가 미입력(IFRS18 2027년 의무시행 전이라 대부분 종목 N/A)")
    return ComponentResult("⑨ IFRS18 투명성", pts, max_points, "; ".join(parts))


def score_commercial_code(data: RawFinancials, max_points: float = 10) -> ComponentResult:
    """미국 상장사는 한국 상법 적용대상이 아니므로(qualitative.py가 US market엔
    애초에 조사를 시도하지 않음) 여기서도 자연히 채점불가(N/A)로 남는다."""
    q = data.commercial_code_benefit_qual
    if q is None:
        return ComponentResult(
            "⑩ 상법개정 수혜", None, max_points,
            "정성평가 미입력 — 채점 불가(미국 상장사는 한국 상법 미적용으로 구조적 N/A)",
        )
    ratio = q.points_awarded / q.max_points if q.max_points else 0
    pts = 10 if ratio >= 0.75 else (5 if ratio >= 0.25 else 2)
    return ComponentResult("⑩ 상법개정 수혜", pts, max_points, q.evidence)


ALL_ITEM_SCORERS = [
    score_roe, score_ocf, score_retained_earnings, score_moat, score_shareholder_return,
    score_pbr, score_debt_ratio, score_capital_allocation, score_ifrs18, score_commercial_code,
]


@dataclass
class PdfOriginalReport:
    ticker: str
    company_name: str
    market: str
    as_of_date: str
    items: list[ComponentResult] = field(default_factory=list)

    @property
    def total_score(self) -> float:
        return sum(c.points_awarded for c in self.items if c.points_awarded is not None)

    @property
    def total_possible(self) -> float:
        return sum(c.max_points for c in self.items)

    @property
    def na_count(self) -> int:
        return sum(1 for c in self.items if c.points_awarded is None)

    @property
    def final_grade(self) -> str:
        """PDF p.37 원문 그대로: 90+ 최상급우량주 / 80+ 우량주 / 70+ 관찰대상.
        70점 미만 등급명은 PDF 원문에 없다(서브에이전트가 39페이지 전체
        확인) — v3와 표기를 맞추기 위해 "미달"이라는 우리측 확장 라벨을
        쓰되, 이는 PDF 원본에 없는 표기임을 리포트에 명시해야 한다."""
        score = self.total_score
        if score >= 90:
            return "최상급우량주"
        if score >= 80:
            return "우량주"
        if score >= 70:
            return "관찰대상"
        return "미달"


def compute_pdf_score(data: RawFinancials) -> PdfOriginalReport:
    items = [scorer(data) for scorer in ALL_ITEM_SCORERS]
    return PdfOriginalReport(
        ticker=data.ticker, company_name=data.company_name, market=data.market,
        as_of_date=data.as_of_date, items=items,
    )
