"""
main.py
일일 파이프라인 오케스트레이터 — 후보군 동적 생성(src/universe.py, 2026-09-28
리오님 확정: "그 시간 기준으로 해당되는 기업을 선정") → 전체 스코어링 →
게이트통과+최고점 선정(src/selection.py) → PDF 생성 → 텔레그램 전송.

=== collector -> RawFinancials 매핑 계층 상태 (2026-09-29) ===
- build_us_report(): 완료, MSFT/AAPL/KO/JPM/BRK-B 등 다수 후보 +
  select_daily_pick() 연동까지 실제 라이브 데이터로 검증됨(키 불필요, yfinance).
- build_kr_report(): 2026-09-29 KRX_AUTH_KEY/DART_API_KEY 모두 확보 후
  삼성전자로 라이브 검증(--diagnose-kr). bsns_year 계산 버그 수정, ROE 매칭
  정상 확인, B/F/G 블록이 구조적으로 0점이던 문제(필드 미연결)를 DART 전체
  재무제표 실측 계정명으로 보강 완료. net_debt_repayment만 아직 미구현.
  자세한 이력은 build_kr_report() 자체 docstring 참고.

=== 후보군 (2026-09-28부터 동적 생성, config/pipeline.yaml + src/universe.py) ===
과거에는 config/watchlist.yaml에 47개 종목을 손으로 큐레이션해 고정해뒀으나,
"매일 실행 시점 기준으로 해당 기업을 선정"하라는 지시에 따라 매 실행마다
src/universe.py가 KR(코스피/코스닥 시가총액 상위 N, KOSPI200/KOSDAQ150 대리
지표) + US(S&P500 전체)를 그 자리에서 새로 조회한다. 상세 배경은
src/universe.py 상단 docstring 참고.

=== 실행에 필요한 환경변수 ===
- KRX_AUTH_KEY, DART_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (전부 사용자 발급)
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import qualitative, universe
from src.collectors import dart_collector, krx_collector, us_collector
from src.notify.telegram_notifier import TelegramNotifyError, send_pdf_report
from src.report.pdf_generator import StockReportEntry, generate_pdf
from src.scoring.dcf import compute_margin_of_safety, compute_wacc
from src.scoring.models import RawFinancials, ScoreReport
from src.scoring.scorecard import compute_score
from src.selection import SelectionResult, record_pick, select_daily_pick

STRESS_TEST_WINDOW_DAYS = 180  # 최근 N일 롤링 구간의 지수 대비 최대낙폭으로 D블록/게이트 평가

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data" / "output"


def _load_pipeline_config() -> dict:
    with open(CONFIG_DIR / "pipeline.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _dart_account(rows: list[dict], sj_div: str, keywords: list[str], amount_key: str = "thstrm_amount") -> float | None:
    """dart_collector.get_financial_account()류 응답에서 sj_div(BS/IS/CF)와 계정명
    키워드로 금액을 찾는다. 못 찾으면 None(지어내지 않음)."""
    for r in rows:
        if r.get("sj_div") != sj_div:
            continue
        name = r.get("account_nm", "")
        if all(k in name for k in keywords):
            v = r.get(amount_key)
            if v in (None, "", "-"):
                continue
            try:
                return float(str(v).replace(",", ""))
            except ValueError:
                continue
    return None


def build_kr_report(candidate: dict, as_of: dt.date, with_qualitative: bool = False) -> ScoreReport:
    """KR 후보 1개를 krx_collector(KRX Open API) + dart_collector(OpenDART)로
    채운 RawFinancials로 채점한다.

    검증 이력: 2026-09-28 작성 당시엔 KRX_AUTH_KEY/DART_API_KEY가 없어 라이브
    미검증 상태였음. 2026-09-29 실제 키로 삼성전자 1종목 라이브 검증
    (--diagnose-kr) 중 두 가지를 발견·수정:
    1) bsns_year(사업보고서 조회 연도) 계산이 뒤집혀 존재할 수 없는 미래
       연도를 조회하던 버그 — 수정 완료.
    2) ROE idx_nm 매칭은 정상 동작 확인. 다만 B(이익의질)·F(가격)·G(주주환원)
       블록에 필요한 필드(ocf_5y/ni_5y/per/pbr/ev_ebit/dividend_total/
       net_buyback_amount/treasury_retirement_ratio_pct)를 애초에 채우지
       않고 있어 이 세 블록이 구조적으로 항상 0점이었음 — DART 전체
       재무제표(get_full_financial_statement)에서 실측 확인한 계정명으로
       전부 채워 넣도록 보강함(아래 코드 주석 참고). net_debt_repayment만
       아직 미구현(차입금 관련 CF 계정 여러 개의 부호 처리를 확정 못해 보류,
       정직하게 None으로 남김 — Shareholder Yield 컴포넌트는 나머지 두
       요소만으로도 부분 계산됨).
    정성 항목(해자/IFRS18/거시)은 with_qualitative=True일 때만 qualitative.py를
    통해 Gemini(Google Search grounding)로 실제 리서치해서 채운다(2026-09-29
    보강) — 기본값 False에서는 여전히 N/A로 둔다. 850종목 전체에 매일 돌리면
    비용·시간이 비현실적이라 select_daily_pick()의 2단계(정량 1차 채점 →
    상위 N만 정성 보강 재채점) 구조에서만 True로 호출되는 게 정상 사용법.
    """
    ticker = candidate["ticker"]
    market = candidate.get("market", "KOSPI")

    end = as_of.strftime("%Y%m%d")
    start = (as_of - dt.timedelta(days=STRESS_TEST_WINDOW_DAYS)).strftime("%Y%m%d")
    index_name = "코스피" if market.upper() == "KOSPI" else "코스닥"
    stress = krx_collector.stress_test_gate(ticker, start, end, market=market, index_name=index_name)

    quote = krx_collector.get_quote(ticker, market=market)
    market_cap = quote["market_cap_krw"]

    corp_code = dart_collector.find_corp_code(ticker)
    # 사업보고서(연간)는 회계연도 종료(12/31) 후 익년 3월말까지 공시된다.
    # 예: FY2025 사업보고서는 2026년 3월에 나온다 — as_of.year의 사업보고서 자체가
    # 존재할 수 없으므로(회계연도가 아직 안 끝났거나 방금 끝나 공시 전) 항상
    # as_of.year - 1(또는 그보다 이전)을 조회해야 한다.
    # - as_of가 4월 이후(공시 시즌 지남) -> 직전 회계연도(year-1) 사업보고서 확정 공시됨
    # - as_of가 1~4월(공시 시즌 중/전) -> 그 직전 회계연도(year-1)는 아직 미공시일 수
    #   있어 안전하게 그 전 연도(year-2) 사용
    # 2026-09-29 실측 검증 과정에서 발견/수정(기존 코드는 year/year-1로 한 해씩 밀려
    # 있어 존재하지 않는 연도를 조회하는 버그였음, 실측 미검증 상태로 방치돼 있었음).
    bsns_year = str(as_of.year - 1 if as_of.month > 4 else as_of.year - 2)

    # 2026-09-29: get_financial_account(주요계정, fnlttSinglAcnt.json)과
    # get_owner_earnings 내부의 get_full_financial_statement(전체 재무제표,
    # fnlttSinglAcntAll.json)을 각각 따로 호출하던 것을 후자 하나로 통합했다.
    # 전체 재무제표가 주요계정의 상위집합이라(이 세션에서 PlayMCP로 삼성전자
    # 실측 확인) 매출액/영업이익/자산총계 등도 여기서 그대로 뽑히고, 주요계정에는
    # 없는 자사주 취득/배당금 지급/현금성자산/당기순이익(IS) 같은 세부 계정까지
    # 같이 얻을 수 있어 API 호출 수가 줄어든다(850종목 규모 실행시간/호출량
    # 리스크 완화에도 도움). 다만 전체 재무제표 쪽이 일부 소형주에서 013(데이터
    # 없음)이 날 가능성이 주요계정보다 높을 수 있어, 그 경우에만 주요계정으로
    # 폴백해 매출/영업이익/재무상태표 3항목만이라도 채운다(현금흐름표 세부
    # 계정은 주요계정에 없어 그 경우 자연히 다시 None으로 남음 — 지어내지 않음).
    #
    # 이 세션에서 PlayMCP로 삼성전자 실제 응답을 직접 조회해 실측 확인한 계정명:
    # - 영업활동현금흐름: account_nm에 "영업활동"+"현금흐름" 포함, sj_div=="CF"
    # - 유형자산/무형자산의 취득: account_nm에 "유형자산"/"무형자산"+"취득", sj_div=="CF"
    # - 자기주식의 취득: account_nm=="자기주식의 취득", sj_div=="CF"
    # - 배당금의 지급: account_nm=="배당금의 지급", sj_div=="CF"
    # - 현금및현금성자산: account_nm=="현금및현금성자산", sj_div=="BS"
    # - 당기순이익: account_nm=="당기순이익", sj_div=="IS"(CF 섹션에도 동명 계정이
    #   있어 sj_div로 먼저 거르는 게 필수 — 안 거르면 잘못된 값을 주울 수 있음)
    try:
        full_rows = dart_collector.get_full_financial_statement(corp_code, bsns_year)
    except dart_collector.DartApiError as e:
        print(f"[{ticker}] 전체 재무제표 조회 실패: {e}")
        full_rows = []

    if not full_rows:
        try:
            full_rows = dart_collector.get_financial_account(corp_code, bsns_year)
            if full_rows:
                print(f"[{ticker}] 전체 재무제표 데이터 없음 — 주요계정으로 폴백"
                      f"(현금흐름표 세부 계정은 이 경로에 없어 관련 항목은 N/A로 남음)")
        except dart_collector.DartApiError as e:
            print(f"[{ticker}] 주요계정 조회도 실패: {e}")

    revenue_3y = [
        _dart_account(full_rows, "IS", ["매출액"], k)
        for k in ("thstrm_amount", "frmtrm_amount", "bfefrmtrm_amount")
    ]
    op_income_3y = [
        _dart_account(full_rows, "IS", ["영업이익"], k)
        for k in ("thstrm_amount", "frmtrm_amount", "bfefrmtrm_amount")
    ]
    ni_3y = [
        _dart_account(full_rows, "IS", ["당기순이익"], k)
        for k in ("thstrm_amount", "frmtrm_amount", "bfefrmtrm_amount")
    ]
    ocf_3y = [
        _dart_account(full_rows, "CF", ["영업활동", "현금흐름"], k)
        for k in ("thstrm_amount", "frmtrm_amount", "bfefrmtrm_amount")
    ]
    total_assets = _dart_account(full_rows, "BS", ["자산총계"])
    total_liabilities = _dart_account(full_rows, "BS", ["부채총계"])
    total_equity = _dart_account(full_rows, "BS", ["자본총계"])
    cash_and_equiv = _dart_account(full_rows, "BS", ["현금및현금성자산"])
    capex_tangible = _dart_account(full_rows, "CF", ["유형자산", "취득"])
    capex_intangible = _dart_account(full_rows, "CF", ["무형자산", "취득"])
    net_buyback_amount = _dart_account(full_rows, "CF", ["자기주식", "취득"])
    dividend_total = _dart_account(full_rows, "CF", ["배당금", "지급"])

    debt_ratio_pct = (total_liabilities / total_equity * 100) if total_liabilities and total_equity else None

    owner_earnings = None
    if ocf_3y[0] is not None:
        capex = (capex_tangible or 0) + (capex_intangible or 0)
        owner_earnings = {"owner_earnings": ocf_3y[0] - capex}
    else:
        cf_names = [r.get("account_nm") for r in full_rows if r.get("sj_div") == "CF"]
        print(f"[{ticker}] 영업활동현금흐름 계정을 찾지 못해 Owner Earnings 계산 불가"
              + (f" — CF 섹션 계정명: {cf_names[:30]}" if cf_names else " (CF 섹션 자체가 없음)"))

    # OCF>NI 이익의질 비교(B블록)용 — 연도별로 짝이 맞는 것만(oldest-first) 사용
    ocf_ni_pairs = [
        (o, n) for o, n in zip(reversed(ocf_3y), reversed(ni_3y))
        if o is not None and n is not None
    ]
    ocf_5y = [o for o, _ in ocf_ni_pairs] or None
    ni_5y = [n for _, n in ocf_ni_pairs] or None

    # PER/PBR: DART 당기순이익/자본총계 + KRX 종가/발행주식수로 직접 계산
    # (krx_collector.get_quote()는 PER/PBR을 KRX 응답에서 직접 못 채워 None으로
    # 반환 중 — 정확한 KRX 투자지표 엔드포인트는 별도 확인 필요한 TODO로 남아있어,
    # 그 전까지는 이렇게 실측 데이터로부터 직접 계산해 채운다)
    per = pbr = ev_ebit = None
    close_price = quote.get("close")
    shares_outstanding = quote.get("shares_outstanding")
    if close_price and shares_outstanding:
        if ni_3y[0] and ni_3y[0] > 0:
            per = round(close_price / (ni_3y[0] / shares_outstanding), 2)
        if total_equity and total_equity > 0:
            pbr = round(close_price / (total_equity / shares_outstanding), 2)
    if market_cap and total_liabilities is not None and cash_and_equiv is not None and op_income_3y[0] and op_income_3y[0] > 0:
        ev = market_cap + total_liabilities - cash_and_equiv
        ev_ebit = round(ev / op_income_3y[0], 2)

    # 자사주 소각비율(G블록, 상법개정/밸류업 수혜 지표) — tesstkAcqusDspsSttus.json의
    # "총계" 행(보통주+우선주) change_qy_incnr(소각으로 인한 감소수량) 합계를
    # 발행주식수로 나눔. 이 세션에서 삼성전자 실제 응답으로 필드명 실측 확인.
    treasury_retirement_ratio_pct = None
    try:
        treasury_rows = dart_collector.get_treasury_stock(corp_code, bsns_year)
        incnr_values = [
            dart_collector._to_number(r.get("change_qy_incnr"))
            for r in treasury_rows if r.get("acqs_mth1") == "총계"
        ]
        incnr_total = sum(v for v in incnr_values if v is not None)
        if incnr_total and shares_outstanding:
            treasury_retirement_ratio_pct = round(incnr_total / shares_outstanding * 100, 3)
    except dart_collector.DartApiError as e:
        print(f"[{ticker}] 자사주 소각현황 조회 실패: {e}")

    roe_series = None
    try:
        idx_rows = dart_collector.get_financial_index(corp_code, bsns_year, idx_cl_code=dart_collector.IDX_PROFITABILITY)
        roe_row = next(
            (r for r in idx_rows if any(k in r.get("idx_nm", "") for k in ["ROE", "자기자본순이익률", "자기자본이익률"])),
            None,
        )
        if roe_row:
            v = roe_row.get("idx_val")
            roe_series = [float(v)] if v not in (None, "", "-") else None
        else:
            print(f"[{ticker}] ROE 지표명 매칭 실패 — 사용가능 idx_nm: "
                  f"{[r.get('idx_nm') for r in idx_rows][:20]}")
    except dart_collector.DartApiError as e:
        print(f"[{ticker}] 재무지표(ROE) 조회 실패: {e}")

    n = sum(1 for v in revenue_3y if v is not None)
    revenue_cagr = None
    if revenue_3y[0] and revenue_3y[-1] and revenue_3y[-1] > 0 and n >= 2:
        periods = n - 1
        revenue_cagr = round(((revenue_3y[0] / revenue_3y[-1]) ** (1 / periods) - 1) * 100, 2)
    op_income_cagr = None
    if op_income_3y[0] and op_income_3y[-1] and op_income_3y[-1] > 0 and n >= 2:
        periods = n - 1
        op_income_cagr = round(((op_income_3y[0] / op_income_3y[-1]) ** (1 / periods) - 1) * 100, 2)

    roic_pct = None
    if op_income_3y[0] and total_assets:
        nopat_approx = op_income_3y[0] * (1 - 0.24)
        roic_pct = nopat_approx / total_assets * 100

    wacc_pct = None
    wacc_note = None
    if market_cap and total_liabilities:
        wacc_pct = compute_wacc(market_cap=market_cap, total_debt=total_liabilities)
        wacc_note = ("표준 가정(Rf=3.5%, Beta=1.0, ERP=5.5%, 세율=24%, 세전타인자본비용=4.0%) "
                     "+ 실측 시가총액/부채총계 기반 자기자본비중(부채총계를 이자부채 근사로 단순화)")

    mos_result_dict = None
    if wacc_pct and market_cap and owner_earnings is not None:
        mos = compute_margin_of_safety(
            fcf_latest=owner_earnings["owner_earnings"],
            fcf_3y_avg=owner_earnings["owner_earnings"],  # DART 단일 호출은 최신연도만 제공 — 3개년 평균은 추가 호출 필요(향후 개선)
            wacc_pct=wacc_pct,
            market_cap=market_cap,
        )
        mos_result_dict = {
            "mos_bear_pct": mos.scenarios[0].margin_of_safety_pct,
            "mos_base_pct": mos.scenarios[1].margin_of_safety_pct,
            "mos_bull_pct": mos.scenarios[2].margin_of_safety_pct,
            "passed": mos.passed,
            "reason": mos.reason,
        }

    moat_qual = ifrs18_segment_disclosed = ifrs18_mpm_qual = macro_qual = None
    if with_qualitative:
        moat_qual = qualitative.research_moat(ticker, candidate["name"], "KR", as_of)
        ifrs18_segment_disclosed, ifrs18_mpm_qual = qualitative.research_ifrs18(
            ticker, candidate["name"], "KR", as_of
        )
        macro_qual = qualitative.research_macro_catalyst(ticker, candidate["name"], "KR", as_of)

    data = RawFinancials(
        ticker=ticker, company_name=candidate["name"], market="KR", as_of_date=as_of.isoformat(),
        roe_5y_pct=roe_series,
        industry_roe_median_pct=None,
        roic_pct=roic_pct,
        wacc_pct=wacc_pct,
        wacc_assumptions_note=wacc_note,
        moat_qual=moat_qual,
        ocf_5y=ocf_5y,
        ni_5y=ni_5y,
        total_assets_latest=total_assets,
        ifrs18_segment_disclosed=ifrs18_segment_disclosed,
        ifrs18_mpm_qual=ifrs18_mpm_qual,
        retained_earnings_series=None,
        debt_ratio_pct=debt_ratio_pct,
        altman_z=None,
        stress_test_result=stress,
        revenue_cagr_3y_pct=revenue_cagr,
        op_income_cagr_3y_pct=op_income_cagr,
        per=per,
        pbr=pbr,
        ev_ebit=ev_ebit,
        industry_per_median=None,
        industry_pbr_median=None,
        dividend_total=dividend_total,
        net_buyback_amount=net_buyback_amount,
        net_debt_repayment=None,  # TODO: CF 차입금 관련 여러 계정(상환/차입 등) 부호 처리 확정 후 추가
        market_cap=market_cap,
        treasury_retirement_ratio_pct=treasury_retirement_ratio_pct,
        macro_qual=macro_qual,
        margin_of_safety_result=mos_result_dict,
    )
    return compute_score(data)


def build_us_report(candidate: dict, as_of: dt.date, with_qualitative: bool = False) -> ScoreReport:
    """US 후보 1개를 us_collector(yfinance, 키 불필요)로 실측 채워 채점한다.

    주의(정직하게 남겨두는 결측): 경제적 해자·IFRS18 MPM 공식화·거시/사이클
    3개 정성 항목(총 17.5/100점)은 여기서 자동으로 채우지 않는다 — 종목별
    사실 근거를 갖춘 정성평가는 리서치가 필요한 작업이라(예: Gemini 내러티브
    생성 단계) 47개 후보 전체에 대해 이 함수가 자체적으로 지어낼 수 없다.
    해당 컴포넌트는 스코어카드에서 N/A로 정직하게 표시된다.
    업종 중앙값(industry_roe_median_pct 등)도 업종 모집단 미확보로 비워둔다
    (weights.yaml에 명시된 대로 PDF 절대컷으로 자동 대체됨)."""
    ticker = candidate["ticker"]

    end = as_of.strftime("%Y-%m-%d")
    start = (as_of - dt.timedelta(days=STRESS_TEST_WINDOW_DAYS)).strftime("%Y-%m-%d")
    stress = us_collector.stress_test_gate(ticker, start, end, index_ticker="^GSPC")

    quote = us_collector.get_quote(ticker)
    fundamentals = us_collector.get_fundamentals_history(ticker)
    try:
        owner_earnings = us_collector.get_owner_earnings(ticker)
    except ValueError as e:
        # 은행/보험 등 금융업종은 전통적 "CapEx" 계정이 없어 Owner Earnings/DCF 방법론
        # 자체가 안 맞는 경우가 흔하다(자본경량 비즈니스) — 억지로 0으로 채우지 않고
        # margin_of_safety_gate를 "판정불가"로 정직하게 남긴다(gates.py가 처리).
        print(f"[{ticker}] Owner Earnings 계산 불가(금융업종 등 CapEx 계정 부재 가능성): {e}")
        owner_earnings = None

    market_cap = quote["market_cap"]
    total_debt = None
    cash_and_equiv = None
    ebit = None
    try:
        bal = us_collector.yf.Ticker(ticker).balance_sheet
        if "Total Debt" in bal.index:
            total_debt = float(bal.loc["Total Debt"].iloc[0])
        for cash_name in ["Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"]:
            if cash_name in bal.index:
                cash_and_equiv = float(bal.loc[cash_name].iloc[0])
                break
    except Exception:  # noqa: BLE001 - WACC 자기자본비중 실측 실패시 표준가정으로 폴백
        pass
    if fundamentals["operating_income"] and fundamentals["operating_income"][0] is not None:
        ebit = fundamentals["operating_income"][0]  # 영업이익을 EBIT 근사치로 사용(이자/법인세 조정 전 단순화)

    dividend_total = net_buyback_amount = net_debt_repayment = None
    try:
        cf = us_collector.yf.Ticker(ticker).cash_flow
        if "Cash Dividends Paid" in cf.index:
            dividend_total = abs(float(cf.loc["Cash Dividends Paid"].iloc[0]))
        if "Repurchase Of Capital Stock" in cf.index:
            net_buyback_amount = abs(float(cf.loc["Repurchase Of Capital Stock"].iloc[0]))
        if "Repayment Of Debt" in cf.index and "Issuance Of Debt" in cf.index:
            repay = abs(float(cf.loc["Repayment Of Debt"].iloc[0]))
            issue = abs(float(cf.loc["Issuance Of Debt"].iloc[0]))
            net_debt_repayment = max(0.0, repay - issue)  # 순유상증자(부채조달 증가)는 주주환원이 아니므로 0으로 하한
    except Exception:  # noqa: BLE001 - Shareholder Yield 서브데이터는 없어도 치명적이지 않음(N/A 처리됨)
        pass

    ev_ebit = None
    if market_cap is not None and total_debt is not None and cash_and_equiv is not None and ebit:
        ev = market_cap + total_debt - cash_and_equiv
        ev_ebit = ev / ebit if ebit else None

    wacc_pct = None
    wacc_note = None
    if market_cap and total_debt is not None:
        wacc_pct = compute_wacc(market_cap=market_cap, total_debt=total_debt)
        wacc_note = ("표준 가정(Rf=3.5%, Beta=1.0, ERP=5.5%, 세율=24%, 세전타인자본비용=4.0%) "
                     "+ 실측 시가총액/총부채 기반 자기자본비중")

    mos_result_dict = None
    if wacc_pct and market_cap and owner_earnings is not None:
        mos = compute_margin_of_safety(
            fcf_latest=owner_earnings["owner_earnings_latest"],
            fcf_3y_avg=owner_earnings["owner_earnings_3y_avg"],
            wacc_pct=wacc_pct,
            market_cap=market_cap,
        )
        mos_result_dict = {
            "mos_bear_pct": mos.scenarios[0].margin_of_safety_pct,
            "mos_base_pct": mos.scenarios[1].margin_of_safety_pct,
            "mos_bull_pct": mos.scenarios[2].margin_of_safety_pct,
            "passed": mos.passed,
            "reason": mos.reason,
        }

    def _cagr(series: list, periods: int) -> float | None:
        """series: 최신이 인덱스0인 리스트. None이 섞여 있으면(은행/보험 등 계정 부재)
        계산하지 않고 None 반환 — 지어내지 않음."""
        if len(series) < 2 or series[0] is None or series[-1] is None or series[-1] <= 0:
            return None
        n_periods = len(series) - 1
        return round(((series[0] / series[-1]) ** (1 / n_periods) - 1) * 100, 2)

    roe_series = [r for r in fundamentals["roe_pct"] if r is not None]
    revenue = fundamentals["revenue"]
    op_income = fundamentals["operating_income"]
    revenue_cagr = _cagr(revenue, len(revenue) - 1)
    op_income_cagr = _cagr(op_income, len(op_income) - 1)

    latest_liabilities = fundamentals["total_liabilities"][0] if fundamentals["total_liabilities"] else None
    latest_equity = fundamentals["equity"][0] if fundamentals["equity"] else None
    debt_ratio_pct = (latest_liabilities / latest_equity * 100) if latest_liabilities and latest_equity else None

    roic_pct = None
    if op_income and op_income[0] is not None and fundamentals["total_assets"]:
        # 간이 ROIC 근사치: NOPAT / (총자산 - 유동부채 근사가 없으므로 총자산 사용) — 이 세션
        # 삼성전자 계산 때와 동일한 단순화(정확한 유동부채 분리는 추가 데이터 필요, 명시함)
        # 은행/보험 등 영업이익 계정이 없는 업종은 자동으로 None 처리됨(지어내지 않음)
        nopat_approx = op_income[0] * (1 - 0.24)
        roic_pct = nopat_approx / fundamentals["total_assets"][0] * 100

    moat_qual = ifrs18_segment_disclosed = ifrs18_mpm_qual = macro_qual = None
    if with_qualitative:
        moat_qual = qualitative.research_moat(ticker, candidate["name"], "US", as_of)
        # 미국 상장사는 US GAAP을 쓰므로 IFRS18(IFRS 전용) 조기적용 여부는
        # research_ifrs18() 프롬프트가 항상 null로 판단하도록 설계돼 있음
        # (qualitative.py 상단 docstring 참고) — 그래도 부문별 분리공시
        # 사실 여부(ifrs18_segment_disclosed)는 US에도 의미가 있어 조사한다.
        ifrs18_segment_disclosed, ifrs18_mpm_qual = qualitative.research_ifrs18(
            ticker, candidate["name"], "US", as_of
        )
        macro_qual = qualitative.research_macro_catalyst(ticker, candidate["name"], "US", as_of)

    data = RawFinancials(
        ticker=ticker, company_name=candidate["name"], market="US", as_of_date=as_of.isoformat(),
        roe_5y_pct=roe_series if roe_series else None,
        industry_roe_median_pct=None,
        roic_pct=roic_pct,
        wacc_pct=wacc_pct,
        wacc_assumptions_note=wacc_note,
        moat_qual=moat_qual,
        ocf_5y=[v for v in fundamentals["ocf"][::-1] if v is not None] or None,
        ni_5y=fundamentals["net_income"][::-1] if fundamentals["net_income"] else None,
        total_assets_latest=fundamentals["total_assets"][0] if fundamentals["total_assets"] else None,
        ifrs18_segment_disclosed=ifrs18_segment_disclosed,
        ifrs18_mpm_qual=ifrs18_mpm_qual,
        retained_earnings_series=None,
        debt_ratio_pct=debt_ratio_pct,
        altman_z=None,
        stress_test_result=stress,
        revenue_cagr_3y_pct=revenue_cagr,
        op_income_cagr_3y_pct=op_income_cagr,
        per=quote["per_trailing"],
        pbr=quote["pbr"],
        ev_ebit=round(ev_ebit, 2) if ev_ebit is not None else None,
        industry_per_median=None,
        industry_pbr_median=None,
        dividend_total=dividend_total,
        net_buyback_amount=net_buyback_amount,
        net_debt_repayment=net_debt_repayment,
        market_cap=market_cap,
        treasury_retirement_ratio_pct=None,
        macro_qual=macro_qual,
        margin_of_safety_result=mos_result_dict,
    )
    return compute_score(data)


def _report_missing_pick(result: SelectionResult) -> str:
    lines = [f"[{result.market}] 오늘의 픽: 없음 — {result.reason}"]
    for i, r in enumerate(result.reference_top3, 1):
        gate_str = "게이트 " + ("통과" if r.all_gates_passed else "미통과")
        lines.append(f"  참고 {i}. {r.company_name}({r.ticker}) {r.total_score:.2f}점, {gate_str}")
    return "\n".join(lines)


def _empty_selection_result(market: str, reason: str) -> SelectionResult:
    return SelectionResult(market=market, picked=None, reference_top3=[], reason=reason)


def run_daily(as_of: dt.date | None = None, dry_run: bool = False) -> Path | None:
    as_of = as_of or dt.date.today()
    cfg = _load_pipeline_config()
    sel_cfg = cfg.get("selection", {})
    cooldown_days = sel_cfg.get("cooldown_days", 10)
    universe_cfg = cfg.get("universe", {})
    kr_cfg = universe_cfg.get("kr", {})

    # 2026-09-29: 정성평가(경제적 해자·IFRS18 MPM·거시) 2단계 재채점 — 850종목
    # 전체에 매일 Gemini를 돌리는 건 비현실적이라, 정량 상위 qualitative_top_n
    # 개(pipeline.yaml selection.qualitative_top_n, 기본 20)에만 적용한다
    # (리오님 지시). GEMINI_API_KEY가 없으면 이 단계 자체를 건너뛴다 — 매
    # 종목마다 실패 로그를 20번 찍는 대신 한 번만 안내하고 정량 채점만 진행.
    qualitative_top_n = sel_cfg.get("qualitative_top_n", 20)
    if os.environ.get("GEMINI_API_KEY"):
        kr_qual_fn = lambda c: build_kr_report(c, as_of, with_qualitative=True)  # noqa: E731
        us_qual_fn = lambda c: build_us_report(c, as_of, with_qualitative=True)  # noqa: E731
    else:
        print(f"[{as_of.isoformat()}] GEMINI_API_KEY 미설정 — 정성평가 2단계 스킵, 정량 채점만 진행")
        kr_qual_fn = us_qual_fn = None

    # 2026-09-29: KR 후보군 생성(get_kr_candidates → KRX 전종목 스냅샷 조회)
    # 자체가 KrxApiError로 죽은 적이 실전에서 실제로 발생했는데(KRX 429),
    # 이게 try/except 없이 그대로 전파되면 이미 정상 동작하는 US 쪽까지
    # 통째로 죽어서 "한국 1 + 미국 1" 중 아무것도 못 보내는 사고가 난다.
    # KR/US를 서로 독립적으로 격리해서, 한쪽이 완전히 죽어도 다른 쪽은
    # 정상적으로 픽을 만들어 전송할 수 있게 한다(사용자 표준 원칙: 없는
    # 데이터를 지어내진 않되, 한쪽의 실패가 다른 쪽 성공까지 막아선 안 됨).
    try:
        kr_candidates = universe.get_kr_candidates(
            as_of,
            top_n_kospi=kr_cfg.get("top_n_kospi", 200),
            top_n_kosdaq=kr_cfg.get("top_n_kosdaq", 150),
        )
    except Exception as e:  # noqa: BLE001
        print(f"[{as_of.isoformat()}] KR 후보군 생성 실패(KR 전체 스킵, US는 계속 진행): {e}")
        kr_candidates = None

    try:
        us_candidates = universe.get_us_candidates()
    except Exception as e:  # noqa: BLE001
        print(f"[{as_of.isoformat()}] US 후보군 생성 실패(US 전체 스킵, KR은 계속 진행): {e}")
        us_candidates = None

    print(
        f"[{as_of.isoformat()}] 후보군 생성 완료: "
        f"KR {len(kr_candidates) if kr_candidates is not None else '실패'}개, "
        f"US {len(us_candidates) if us_candidates is not None else '실패'}개"
    )

    if kr_candidates is not None:
        try:
            kr_result = select_daily_pick(
                "KR", kr_candidates,
                score_fn=lambda c: build_kr_report(c, as_of),
                as_of=as_of, cooldown_days=cooldown_days,
                qualitative_score_fn=kr_qual_fn, qualitative_top_n=qualitative_top_n,
            )
        except Exception as e:  # noqa: BLE001
            print(f"[{as_of.isoformat()}] KR 채점 단계 실패(KR 전체 스킵, US는 계속 진행): {e}")
            kr_result = _empty_selection_result("KR", f"KR 채점 단계 실패: {e}")
    else:
        kr_result = _empty_selection_result("KR", "KR 후보군 생성 실패로 채점 자체를 시도하지 못함")

    if us_candidates is not None:
        try:
            us_result = select_daily_pick(
                "US", us_candidates,
                score_fn=lambda c: build_us_report(c, as_of),
                as_of=as_of, cooldown_days=cooldown_days,
                qualitative_score_fn=us_qual_fn, qualitative_top_n=qualitative_top_n,
            )
        except Exception as e:  # noqa: BLE001
            print(f"[{as_of.isoformat()}] US 채점 단계 실패(US 전체 스킵, KR은 계속 진행): {e}")
            us_result = _empty_selection_result("US", f"US 채점 단계 실패: {e}")
    else:
        us_result = _empty_selection_result("US", "US 후보군 생성 실패로 채점 자체를 시도하지 못함")

    print(f"[{as_of.isoformat()}] {kr_result.reason}")
    print(f"[{as_of.isoformat()}] {us_result.reason}")

    if kr_result.picked is None:
        print(_report_missing_pick(kr_result))
    if us_result.picked is None:
        print(_report_missing_pick(us_result))

    entries = []
    if kr_result.picked is not None:
        entries.append(StockReportEntry(report=kr_result.picked))
    if us_result.picked is not None:
        entries.append(StockReportEntry(report=us_result.picked))

    if not entries:
        print("오늘은 KR/US 모두 게이트 통과 픽이 없어 PDF를 생성하지 않습니다.")
        return None

    out_path = OUTPUT_DIR / f"report_{as_of.isoformat()}.pdf"
    generate_pdf(entries, as_of_date=as_of.isoformat(), output_path=out_path)
    print(f"PDF 생성 완료: {out_path}")

    if kr_result.picked is not None:
        record_pick("KR", kr_result.picked.ticker, as_of)
    if us_result.picked is not None:
        record_pick("US", us_result.picked.ticker, as_of)

    if not dry_run:
        caption_parts = []
        if kr_result.picked:
            caption_parts.append(f"KR: {kr_result.picked.company_name}")
        if us_result.picked:
            caption_parts.append(f"US: {us_result.picked.company_name}")
        try:
            send_pdf_report(
                out_path,
                caption=f"Quality Stock Daily {as_of.isoformat()} — " + " / ".join(caption_parts),
            )
            print("텔레그램 전송 완료")
        except TelegramNotifyError as e:
            print(f"텔레그램 전송 실패(리포트 파일은 생성됨): {e}", file=sys.stderr)
            raise

    return out_path


def _diagnose_kr_report(as_of: dt.date) -> None:
    """build_kr_report()를 삼성전자 1종목으로만 실행해 KRX+DART 전체 연동을
    빠르게 점검한다(850종목 전체를 도는 run_daily()와 달리 가볍게, 1종목만).
    ROE idx_nm 매칭 등 DART 의존 부분이 실제 키로 정상 동작하는지 확인하는
    용도 — 2026-09-29 KRX_AUTH_KEY/DART_API_KEY 모두 확보 후 신설."""
    import json

    candidate = {"ticker": "005930", "name": "삼성전자", "market": "KOSPI"}
    print(f"=== build_kr_report() 진단: {candidate['name']}({candidate['ticker']}), as_of={as_of} ===")
    report = build_kr_report(candidate, as_of)
    print(json.dumps(
        {
            "ticker": report.ticker,
            "company_name": report.company_name,
            "market": report.market,
            "as_of_date": report.as_of_date,
            "total_score": report.total_score,
            "total_possible": report.total_possible,
            "all_gates_passed": report.all_gates_passed,
            "final_grade": report.final_grade,
            "blocks": [
                {
                    "key": b.block_key, "name": b.name,
                    "points_awarded": b.points_awarded, "max_points": b.max_points,
                    "components": [
                        {"name": c.name, "points_awarded": c.points_awarded, "max_points": c.max_points, "note": c.note}
                        for c in b.components
                    ],
                }
                for b in report.blocks
            ],
            "gates": [
                {"key": g.gate_key, "passed": g.passed, "reason": g.reason}
                for g in report.gates
            ],
        },
        ensure_ascii=False, indent=2,
    ))


def _diagnose_qualitative(as_of: dt.date) -> None:
    """2026-09-29 신설: 정성평가 3항목(qualitative.py, GEMINI_API_KEY 필요)이
    실제로 동작하는지 삼성전자(KR)·마이크로소프트(US) 2종목만으로 가볍게
    점검한다.

    2026-09-29 수정: 처음엔 build_kr_report(with_qualitative=True)를 통째로
    호출했는데, 그 안에 있는 D블록 스트레스테스트(krx_collector, ~130영업일
    조회)가 그날 KRX 429 문제와 맞물려 정성평가 코드가 실행되기도 전에 죽는
    문제가 실제로 발생함(test-qualitative.yml 1차 실행 실패 원인). 정성평가는
    KRX/DART와 무관한 기능이라, qualitative.py 함수를 KRX/DART 경유 없이
    직접 호출해 Gemini 연동만 순수하게 점검하도록 분리했다."""
    import json

    for market, ticker, name in (("KR", "005930", "삼성전자"), ("US", "MSFT", "Microsoft")):
        print(f"\n=== 정성평가 진단: {name}({ticker}, {market}) ===")
        moat = qualitative.research_moat(ticker, name, market, as_of)
        segment_disclosed, mpm_qual = qualitative.research_ifrs18(ticker, name, market, as_of)
        macro = qualitative.research_macro_catalyst(ticker, name, market, as_of)
        result = {
            "moat": {"points": moat.points_awarded, "max": moat.max_points, "evidence": moat.evidence} if moat else None,
            "ifrs18_segment_disclosed": segment_disclosed,
            "ifrs18_mpm_qual": (
                {"points": mpm_qual.points_awarded, "max": mpm_qual.max_points, "evidence": mpm_qual.evidence}
                if mpm_qual else None
            ),
            "macro": {"points": macro.points_awarded, "max": macro.max_points, "evidence": macro.evidence} if macro else None,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="quality-stock-daily 일일 파이프라인 실행")
    parser.add_argument("--dry-run", action="store_true", help="PDF만 생성하고 텔레그램 전송은 생략")
    parser.add_argument("--date", type=str, default=None, help="YYYY-MM-DD (기본값: 오늘)")
    parser.add_argument(
        "--diagnose-kr", action="store_true",
        help="전체 파이프라인 대신 build_kr_report()를 삼성전자 1종목으로만 실행해 KRX+DART 연동만 점검",
    )
    parser.add_argument(
        "--diagnose-qualitative", action="store_true",
        help="삼성전자+Microsoft 2종목으로 정성평가(qualitative.py, GEMINI_API_KEY 필요) 연동만 점검",
    )
    args = parser.parse_args()

    target_date = dt.date.fromisoformat(args.date) if args.date else dt.date.today()

    if args.diagnose_kr:
        _diagnose_kr_report(target_date)
    elif args.diagnose_qualitative:
        _diagnose_qualitative(target_date)
    else:
        run_daily(as_of=target_date, dry_run=args.dry_run)
