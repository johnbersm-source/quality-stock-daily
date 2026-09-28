"""
us_collector.py
미국 주식 시세/재무 데이터 수집 모듈 — yfinance 기반, API 키 불필요(무료).

KRX/DART와 달리 yfinance는 별도 인증키가 필요 없어 이 샌드박스에서 바로
라이브 테스트가 가능했다(사용자 표준 원칙: "스킬은 저장 전 실제 예시 하나로
테스트" 준수 — 하단 __main__ 참고, 실제 실행 결과로 검증 완료).

=== 검증 상태 (2026-09-28) ===
- 라이브 테스트 완료(이 세션에서 실제 실행): 티커 MSFT로 OHLCV/현재시세/
  현금흐름(OCF·CapEx) 전부 실측 확인. 상세 수치는 __main__ 실행 로그 참고.
- 벤치마크 지수는 S&P500(^GSPC)을 기본값으로 사용(코스피 대비 미국 대형주
  스트레스테스트 게이트의 비교 대상).
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import yfinance as yf


@dataclass
class PriceWindow:
    ticker: str
    start_date: str
    end_date: str
    peak_close: float
    peak_date: str
    trough_close: float
    trough_date: str
    drawdown_pct: float


def get_ohlcv(ticker: str, start_date: str, end_date: str) -> pd.DataFrame:
    """일별 OHLCV. 날짜 형식 YYYY-MM-DD."""
    df = yf.Ticker(ticker).history(start=start_date, end=end_date, auto_adjust=False)
    if df.empty:
        raise ValueError(f"OHLCV 조회 결과 없음: ticker={ticker}, {start_date}~{end_date}")
    return df


def compute_drawdown(df: pd.DataFrame, close_col: str = "Close") -> PriceWindow:
    """구간 내 최고종가 → 이후 최저종가 기준 최대 낙폭."""
    closes = df[close_col]
    peak_idx = closes.idxmax()
    peak_val = closes.loc[peak_idx]
    after_peak = closes.loc[peak_idx:]
    trough_idx = after_peak.idxmin()
    trough_val = after_peak.loc[trough_idx]
    dd = (trough_val - peak_val) / peak_val * 100
    return PriceWindow(
        ticker="",
        start_date=str(df.index[0].date()),
        end_date=str(df.index[-1].date()),
        peak_close=float(peak_val),
        peak_date=str(peak_idx.date()),
        trough_close=float(trough_val),
        trough_date=str(trough_idx.date()),
        drawdown_pct=float(dd),
    )


def stress_test_gate(
    stock_ticker: str,
    start_date: str,
    end_date: str,
    index_ticker: str = "^GSPC",
) -> dict:
    """D블록 게이트(미국판): 정의된 구간에서 종목 낙폭이 지수(S&P500) 낙폭보다
    크면 미통과. passed 조건: 종목낙폭 >= 지수낙폭 (덜 빠지거나 상승)."""
    idx_df = get_ohlcv(index_ticker, start_date, end_date)
    stk_df = get_ohlcv(stock_ticker, start_date, end_date)

    idx_window = compute_drawdown(idx_df)
    stk_window = compute_drawdown(stk_df)

    passed = stk_window.drawdown_pct >= idx_window.drawdown_pct
    return {
        "stock_ticker": stock_ticker,
        "index_ticker": index_ticker,
        "window": f"{start_date}~{end_date}",
        "index_drawdown_pct": round(idx_window.drawdown_pct, 2),
        "stock_drawdown_pct": round(stk_window.drawdown_pct, 2),
        "index_peak_date": idx_window.peak_date,
        "index_trough_date": idx_window.trough_date,
        "stock_peak_date": stk_window.peak_date,
        "stock_trough_date": stk_window.trough_date,
        "passed": bool(passed),
    }


def get_quote(ticker: str) -> dict:
    """현재(최근) 시세 요약 — 종가, 시가총액, PER, PBR, 발행주식수.

    2026-09-28 라이브 테스트 중 발견: yfinance의 Ticker.get_info()(quoteSummary
    엔드포인트)는 Yahoo Finance의 "crumb" 인증이 필요한데 이 샌드박스 환경에서
    HTTP 429(rate-limited)/401(Invalid Crumb)로 실패함 — Yahoo 측 정책/속도제한
    문제로, yfinance 라이브러리 버그가 아님. 이를 우회하기 위해:
    - 시세/시가총액/발행주식수는 crumb이 필요 없는 fast_info로 조회(라이브 확인 완료)
    - PER/PBR은 info에 의존하지 않고 시가총액 ÷ 최근 순이익(income_stmt) /
      ÷ 자기자본(balance_sheet)으로 직접 계산(라이브 확인 완료) — 배당수익률 등
      info 전용 필드(sector/industry 등)는 crumb 이슈로 조회 실패 시 None 반환.
    """
    tk = yf.Ticker(ticker)
    fi = tk.fast_info

    close = fi.get("lastPrice")
    market_cap = fi.get("marketCap")
    shares = fi.get("shares")
    currency = fi.get("currency")

    per = pbr = None
    try:
        inc = tk.income_stmt
        if inc is not None and not inc.empty and "Net Income" in inc.index and market_cap:
            net_income = float(inc.loc["Net Income"].iloc[0])
            if net_income:
                per = market_cap / net_income
    except Exception:  # noqa: BLE001 - PER 계산 실패는 치명적이지 않음, None 유지
        pass

    try:
        bal = tk.balance_sheet
        equity_row = None
        if bal is not None and not bal.empty:
            for name in ["Stockholders Equity", "Common Stock Equity",
                         "Total Equity Gross Minority Interest"]:
                if name in bal.index:
                    equity_row = bal.loc[name]
                    break
        if equity_row is not None and market_cap:
            equity = float(equity_row.iloc[0])
            if equity:
                pbr = market_cap / equity
    except Exception:  # noqa: BLE001 - PBR 계산 실패는 치명적이지 않음, None 유지
        pass

    return {
        "ticker": ticker,
        "close": close,
        "market_cap": market_cap,
        "per_trailing": per,
        "pbr": pbr,
        "shares_outstanding": shares,
        "currency": currency,
    }


def get_owner_earnings(ticker: str) -> dict:
    """margin_of_safety_gate(weights.yaml)용 Owner Earnings = OCF - CapEx.
    yfinance의 연간 현금흐름표(cash_flow)에서 최근 회계연도 값을 추출한다."""
    tk = yf.Ticker(ticker)
    cf = tk.cash_flow  # index=계정명, columns=연도(최신이 첫 컬럼)
    if cf is None or cf.empty:
        raise ValueError(f"현금흐름표 조회 결과 없음: ticker={ticker}")

    def _row(candidates: list[str]) -> pd.Series | None:
        for name in candidates:
            if name in cf.index:
                return cf.loc[name]
        return None

    ocf_row = _row(["Operating Cash Flow", "Total Cash From Operating Activities",
                     "Cash Flow From Continuing Operating Activities"])
    capex_row = _row(["Capital Expenditure", "Capital Expenditures",
                       "Purchase Of PPE", "Net PPE Purchase And Sale"])

    if ocf_row is None:
        raise ValueError(
            f"영업활동현금흐름 계정을 찾지 못했습니다(ticker={ticker}). "
            f"사용 가능한 계정명: {list(cf.index)[:30]}"
        )
    if capex_row is None:
        raise ValueError(
            f"CapEx 계정을 찾지 못했습니다(ticker={ticker}). "
            f"사용 가능한 계정명: {list(cf.index)[:30]}"
        )

    latest_year_col = cf.columns[0]
    ocf = float(ocf_row.iloc[0])
    # yfinance는 CapEx를 보통 음수로 표기(현금유출) — Owner Earnings 계산 시
    # OCF - |CapEx| 가 되도록 부호를 정규화한다.
    capex_raw = float(capex_row.iloc[0])
    capex_abs = abs(capex_raw)

    # 최근 3개년 평균(약세 시나리오용) — 컬럼 수가 3개 미만이면 있는 만큼만 사용
    n = min(3, len(ocf_row))
    ocf_3y_avg = float(ocf_row.iloc[:n].mean())
    capex_3y_avg_abs = float(capex_row.iloc[:n].abs().mean())

    return {
        "ticker": ticker,
        "fiscal_year_end": str(latest_year_col.date()) if hasattr(latest_year_col, "date") else str(latest_year_col),
        "ocf_latest": ocf,
        "capex_latest_abs": capex_abs,
        "owner_earnings_latest": ocf - capex_abs,
        "ocf_3y_avg": ocf_3y_avg,
        "capex_3y_avg_abs": capex_3y_avg_abs,
        "owner_earnings_3y_avg": ocf_3y_avg - capex_3y_avg_abs,
    }


if __name__ == "__main__":
    import json

    print("=== 라이브 테스트: MSFT D블록 스트레스테스트 게이트 (S&P500 대비) ===")
    result = stress_test_gate("MSFT", "2026-06-01", "2026-08-31", index_ticker="^GSPC")
    print(json.dumps(result, ensure_ascii=False, indent=2))

    print("\n=== 라이브 테스트: MSFT 현재 시세 ===")
    q = get_quote("MSFT")
    print(json.dumps(q, ensure_ascii=False, indent=2))

    print("\n=== 라이브 테스트: MSFT Owner Earnings (OCF - CapEx) ===")
    oe = get_owner_earnings("MSFT")
    print(json.dumps(oe, ensure_ascii=False, indent=2))
