"""
universe.py
매일 스코어링할 KR/US 후보군을 "그날 시점 기준으로" 동적으로 만든다
(2026-09-28, 리오님 확정 지시: "후보종목을 미리 선정해서 하는게 아니라 매일
보고서를 만들어 보낼때 그 시간 기준으로 해당되는 기업을 선정해서 보고서를
맞춰서 작성해야해"). config/watchlist.yaml에 47개 종목을 손으로 큐레이션해
박아뒀던 이전 설계(초안)는 틀렸다 — 이 모듈이 그걸 대체한다.

=== 후보군 범위 (2026-09-28 리오님과 재확정, 최초 결정은 /areas 메모 참고) ===
- KR: 원래 목표는 "KOSPI200 + KOSDAQ150" 공식 지수 구성종목이었으나, 조사 결과
  공식 KRX Open API(openapi.krx.co.kr, 이미 승인받은 KRX_AUTH_KEY)에는 지수
  구성종목 조회 기능이 아예 없고, 실제 구성종목 데이터는 data.krx.co.kr의
  비공식 내부 API에서만 제공되는데 이 경로는 2024-12-27부터 로그인
  (KRX_ID/PW) 의무화됨(봇탐지 목적) — krx_collector.py가 바로 이 문제 때문에
  pykrx 방식을 버리고 공식 키 API로 전면 재작성됐던 전례가 있다(모듈 상단
  히스토리 참고). 매일 GitHub Actions에서 개인 계정 로그인을 자동화하는 건
  계정 제한/정지 위험이 있어, 정확한 지수 구성종목 대신 "코스피 시가총액
  상위 200 + 코스닥 시가총액 상위 150"을 대리 지표(proxy)로 쓰기로
  리오님과 합의했다. 이미 승인된 공식 API만 사용하므로 추가 키/로그인 불필요.
- US: S&P500은 공식 API 없이도 자주 갱신되는 공개 표(위키피디아)가 있어
  실제 지수 구성종목을 그대로 쓴다(대리 지표 아님) — us_collector.get_sp500_constituents().

=== 규모/실행시간 관련 알려진 리스크 (2026-09-28, 리오님께 보고 필요) ===
후보군이 기존 47개(KR 23 + US 24)에서 KR ~350개 + US ~500개 = 총 ~850개로
대폭 늘었다. 종목당 DART 호출 수는 10,000건/일 한도 대비 여유 있지만
(350 × ~6회 ≈ 2,100건), yfinance는 공식 API가 아니라 비공식 엔드포인트라
이미 24개 종목 테스트에서도 429(rate limit) 경험이 있어 500개 규모에서는
실행시간·안정성 리스크가 실제로 있다. 1차 라이브 실행(GitHub Actions,
네트워크 제약 없는 환경) 결과를 보고 필요시 사전 스크리닝(예: 데이터 확보
가능 여부만 먼저 확인 후 상세 스코어링은 일부만) 추가를 검토해야 한다.
지금은 GitHub Actions workflow의 timeout-minutes를 90분으로 늘려 1차
실행이 중간에 잘리지 않게만 해뒀다.
"""
from __future__ import annotations

import datetime as dt

from src.collectors import krx_collector, us_collector


def get_kr_candidates(
    as_of: dt.date,
    top_n_kospi: int = 200,
    top_n_kosdaq: int = 150,
) -> list[dict]:
    """그날(as_of) 기준 코스피 시가총액 상위 top_n_kospi + 코스닥 시가총액 상위
    top_n_kosdaq 종목을 합쳐 후보군으로 반환한다. 각 원소는 build_kr_report()가
    기대하는 {"ticker", "name", "market"} 형태."""
    ref_date = as_of.strftime("%Y%m%d")

    kospi = krx_collector.get_top_n_by_market_cap("KOSPI", top_n_kospi, ref_date=ref_date)
    kosdaq = krx_collector.get_top_n_by_market_cap("KOSDAQ", top_n_kosdaq, ref_date=ref_date)

    candidates = [
        {"ticker": r["ticker"], "name": r["name"], "market": "KOSPI"} for r in kospi
    ] + [
        {"ticker": r["ticker"], "name": r["name"], "market": "KOSDAQ"} for r in kosdaq
    ]
    return candidates


def get_us_candidates() -> list[dict]:
    """S&P500 전체 구성종목을 후보군으로 반환한다. 각 원소는 build_us_report()가
    기대하는 {"ticker", "name"} 형태(sector는 참고용으로 같이 담아둠)."""
    sp500 = us_collector.get_sp500_constituents()
    return [{"ticker": r["ticker"], "name": r["name"], "sector": r["sector"]} for r in sp500]


if __name__ == "__main__":
    import json

    today = dt.date.today()
    print(f"=== KR 후보군 (as_of={today.isoformat()}) ===")
    try:
        kr = get_kr_candidates(today)
        print(f"{len(kr)}개. 첫 5개: {json.dumps(kr[:5], ensure_ascii=False)}")
    except Exception as e:  # noqa: BLE001 - 진단용 __main__ 블록
        print(f"KR 후보군 조회 실패(KRX_AUTH_KEY 필요): {e}")

    print("\n=== US 후보군 (S&P500) ===")
    us = get_us_candidates()
    print(f"{len(us)}개. 첫 5개: {json.dumps(us[:5], ensure_ascii=False)}")
