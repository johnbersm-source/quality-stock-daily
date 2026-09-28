"""
test_universe.py
src/universe.py의 후보군 조합 로직(KOSPI+KOSDAQ 병합, US 변환)을
collector 함수를 monkeypatch로 목킹해 네트워크 호출 없이 검증한다.

주의: 이건 "동적 후보군을 만드는 조합/변환 로직 자체"의 단위테스트다.
실제 KRX_AUTH_KEY로 krx_collector.get_top_n_by_market_cap()이 진짜 올바른
데이터를 반환하는지는 라이브 키가 있어야 확인 가능(README 참고). US 쪽
us_collector.get_sp500_constituents()는 이 세션에서 실측 라이브 검증 완료
(503개 확인, BRK.B -> BRK-B 정규화 확인).
"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import universe  # noqa: E402
from src.collectors import krx_collector, us_collector  # noqa: E402


def run_test():
    as_of = dt.date(2026, 9, 28)

    # --- KR: 코스피/코스닥 시가총액 상위 목킹 ---
    def fake_top_n(market, n, ref_date=None):
        if market == "KOSPI":
            rows = [
                {"ticker": "005930", "name": "삼성전자", "market_cap": 500_000_000_000_000},
                {"ticker": "000660", "name": "SK하이닉스", "market_cap": 200_000_000_000_000},
            ]
        else:
            rows = [
                {"ticker": "247540", "name": "에코프로비엠", "market_cap": 10_000_000_000_000},
            ]
        assert ref_date == as_of.strftime("%Y%m%d"), "as_of 날짜가 YYYYMMDD로 그대로 전달돼야 함"
        return rows[:n]

    orig_top_n = krx_collector.get_top_n_by_market_cap
    krx_collector.get_top_n_by_market_cap = fake_top_n
    try:
        kr = universe.get_kr_candidates(as_of, top_n_kospi=2, top_n_kosdaq=1)
    finally:
        krx_collector.get_top_n_by_market_cap = orig_top_n

    assert len(kr) == 3, f"KOSPI 2개 + KOSDAQ 1개 = 3개여야 하는데 {len(kr)}개"
    assert {"ticker", "name", "market"} <= kr[0].keys(), "build_kr_report()가 기대하는 키(ticker/name/market)가 있어야 함"
    kospi_tickers = {c["ticker"] for c in kr if c["market"] == "KOSPI"}
    kosdaq_tickers = {c["ticker"] for c in kr if c["market"] == "KOSDAQ"}
    assert kospi_tickers == {"005930", "000660"}, f"KOSPI 티커 불일치: {kospi_tickers}"
    assert kosdaq_tickers == {"247540"}, f"KOSDAQ 티커 불일치: {kosdaq_tickers}"
    print(f"[KR PASS] {len(kr)}개 후보 생성, market 태깅 정상: {kr}")

    # --- US: S&P500 스크래핑 결과 -> 후보 dict 변환 목킹 ---
    def fake_sp500():
        return [
            {"ticker": "MSFT", "name": "Microsoft", "sector": "Technology"},
            {"ticker": "BRK-B", "name": "Berkshire Hathaway", "sector": "Financials"},
        ]

    orig_sp500 = us_collector.get_sp500_constituents
    us_collector.get_sp500_constituents = fake_sp500
    try:
        us = universe.get_us_candidates()
    finally:
        us_collector.get_sp500_constituents = orig_sp500

    assert len(us) == 2
    assert {"ticker", "name", "sector"} <= us[0].keys(), "build_us_report()가 기대하는 키(ticker/name)가 있어야 함"
    assert us[1]["ticker"] == "BRK-B", "위키 표기(BRK.B)가 아니라 yfinance 표기(BRK-B)로 이미 정규화된 채 들어와야 함"
    print(f"[US PASS] {len(us)}개 후보 생성: {us}")

    print("\nALL PASS")


if __name__ == "__main__":
    run_test()
