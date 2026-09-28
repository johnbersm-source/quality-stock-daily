"""
krx_collector.py
한국거래소(KRX) 시세/지수 데이터 수집 모듈 — KRX 정보데이터시스템 Open API 기반.

=== 2026-09-28 아키텍처 변경 이력 (중요) ===
원래 pykrx(무료, 키 불필요)로 구현했으나, KRX가 2024-12-27 정책 변경으로
data.krx.co.kr 크롤링 방식(pykrx 내부 동작 방식)에 로그인을 의무화함
(봇 스크래핑 방지 목적, sharebook-kr/pykrx#244 이슈로 공식 확인).
따라서 이 모듈은 KRX가 공식 제공하는 키 기반 REST API
(openapi.krx.co.kr / data-dbg.krx.co.kr)로 전면 재작성했다.

=== 사용 전 필수 준비 ===
1. https://openapi.krx.co.kr 에서 회원가입 + 이용약관 동의 후 AUTH_KEY 발급(무료, 1년 유효, 갱신가능)
2. 발급받은 키를 환경변수 KRX_AUTH_KEY 로 설정
3. **중요**: AUTH_KEY를 발급받아도 서비스(엔드포인트)별로 별도 "API 이용신청" 승인이 필요함.
   미승인 서비스 호출 시 401/403 응답 — 키 자체가 잘못된 게 아니라 그 서비스가 아직
   승인되지 않은 것이므로 openapi.krx.co.kr 마이페이지에서 개별 신청 필요.
4. 비상업적 이용 기준 무료, 1일 10,000건 호출 제한.

=== 검증 상태 (2026-09-28) ===
- 엔드포인트 リ처빌리티: curl로 직접 확인 완료 — 잘못된 키로 요청 시
  HTTP 401 + {"respMsg":"Unauthorized Key","respCode":"401"} 정상 응답 확인.
  → API 서버 자체는 정상 동작 중.
- 필드명(컬럼명): 커뮤니티 예제(skkimjj/krx-openapi-examples)에서 확인한
  ISU_CD, LIST_SHRS, BAS_DD 외 나머지 필드명(TDD_CLSPRC 등)은 KRX Open API의
  통상적 명명 규칙을 따른 "최선 추정"이며, 실제 유효한 AUTH_KEY로
  라이브 호출해 확인 전까지는 미검증 상태임 — 이 파일의 __main__ 블록에
  raw JSON을 그대로 출력하는 진단 모드를 포함해, 키 발급 후 1회 실행으로
  필드명을 바로 검증/수정할 수 있게 했다.
- PER/PBR: 이 일별매매정보 서비스(sto/stk_bydd_trd)에는 포함되어 있지 않을
  가능성이 높음(투자지표는 별도 서비스로 추정되나 정확한 서비스 경로 미확인).
  따라서 get_quote()는 종가/시가총액/상장주식수만 KRX Open API로 채우고,
  PER/PBR은 None으로 두거나 이 세션에서 이미 검증된 PlayMCP koreaStock 계열
  도구로 보완하는 것을 권장(파이프라인 실행 시점엔 PlayMCP를 쓸 수 없으므로
  별도 소스 필요 — 미해결 항목으로 남겨둠, 사용자에게 별도 보고 필요).
"""
from __future__ import annotations

import datetime as dt
import os
import time
from dataclasses import dataclass

import requests

BASE_URL = "https://data-dbg.krx.co.kr/svc/apis/{}"

# 서비스 경로 상수 (skkimjj/krx-openapi-examples 에서 확인)
SVC_KOSPI_STOCKS = "sto/stk_bydd_trd"     # 유가증권 일별매매정보
SVC_KOSDAQ_STOCKS = "sto/ksq_bydd_trd"    # 코스닥 일별매매정보
SVC_KOSPI_INDEX = "idx/kospi_dd_trd"      # 코스피 지수 일별시세
SVC_KOSDAQ_INDEX = "idx/kosdaq_dd_trd"    # 코스닥 지수 일별시세 (명칭 추정, 미검증)

# 종목코드 → 시장 판별에 쓰이는 매우 단순한 휴리스틱은 쓰지 않는다.
# 코스피/코스닥 여부는 호출하는 쪽(scoring 모듈)이 알고 있어야 하며,
# 이 모듈은 시장 인자를 명시적으로 받는다.


class KrxApiError(Exception):
    """KRX Open API 관련 오류(키 미설정, 401/403, 예상치 못한 응답 구조 등)."""


@dataclass
class PriceWindow:
    """스트레스테스트용 특정 구간의 가격 요약."""
    ticker: str
    start_date: str
    end_date: str
    peak_close: float
    peak_date: str
    trough_close: float
    trough_date: str
    drawdown_pct: float  # (trough-peak)/peak * 100, 음수


def _auth_key() -> str:
    key = os.environ.get("KRX_AUTH_KEY")
    if not key:
        raise KrxApiError(
            "KRX_AUTH_KEY 환경변수가 설정되지 않았습니다. "
            "https://openapi.krx.co.kr 에서 무료 발급 후 설정하세요."
        )
    return key


def _fetch_day(service: str, bas_dd: str, max_retries: int = 2) -> list[dict]:
    """단일 영업일 데이터를 조회한다. KRX Open API는 basDd(YYYYMMDD) 하루 단위로만
    조회 가능하므로, 기간 조회는 상위 함수에서 날짜별로 반복 호출한다.

    반환: OutBlock_1 리스트(행 딕셔너리들). 주말/휴장일이면 빈 리스트(정상 상황).
    """
    url = BASE_URL.format(service)
    headers = {"AUTH_KEY": _auth_key(), "Accept": "application/json"}
    params = {"basDd": bas_dd}

    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=15)
        except requests.RequestException as e:
            last_err = e
            time.sleep(1)
            continue

        if resp.status_code in (401, 403):
            raise KrxApiError(
                f"KRX Open API 인증/권한 오류(HTTP {resp.status_code}): {resp.text[:300]}\n"
                f"→ AUTH_KEY 자체가 잘못됐거나, 서비스 '{service}'에 대한 "
                f"'API 이용신청' 승인이 아직 안 된 상태일 수 있습니다. "
                f"openapi.krx.co.kr 마이페이지에서 해당 서비스 신청 여부를 확인하세요."
            )
        if resp.status_code != 200:
            raise KrxApiError(
                f"KRX Open API 오류(HTTP {resp.status_code}), service={service}, "
                f"basDd={bas_dd}: {resp.text[:300]}"
            )

        try:
            payload = resp.json()
        except ValueError as e:
            raise KrxApiError(
                f"KRX Open API 응답이 JSON이 아닙니다(service={service}, basDd={bas_dd}): "
                f"{resp.text[:300]}"
            ) from e

        # 정상 응답 구조: {"OutBlock_1": [ {...}, ... ]}
        # 데이터 없는 날(주말/휴장일/조회당일 미정산 등)은 OutBlock_1이 빈 리스트이거나
        # 키 자체가 없을 수 있음 — 둘 다 "정상적인 0건"으로 처리한다.
        rows = payload.get("OutBlock_1")
        if rows is None:
            # 예상치 못한 응답 구조 — 진단을 위해 raw payload를 노출
            raise KrxApiError(
                f"예상치 못한 응답 구조(OutBlock_1 없음), service={service}, "
                f"basDd={bas_dd}. raw={str(payload)[:500]}"
            )
        return rows

    raise KrxApiError(f"네트워크 오류로 {max_retries + 1}회 시도 후 실패: {last_err}")


def _business_days(start_date: str, end_date: str):
    """YYYYMMDD 문자열 구간 내 월~금(공휴일은 걸러내지 않음 — 공휴일은 0건 응답으로
    자연스럽게 스킵됨)을 하루씩 생성한다."""
    d0 = dt.datetime.strptime(start_date, "%Y%m%d").date()
    d1 = dt.datetime.strptime(end_date, "%Y%m%d").date()
    d = d0
    while d <= d1:
        if d.weekday() < 5:  # 0=월 ... 4=금
            yield d.strftime("%Y%m%d")
        d += dt.timedelta(days=1)


def get_ohlcv(ticker: str, start_date: str, end_date: str, market: str = "KOSPI") -> list[dict]:
    """일별 OHLCV 리스트를 날짜 오름차순으로 반환한다(종목코드로 필터링).

    market: "KOSPI" 또는 "KOSDAQ" — 종목이 어느 시장인지 호출자가 알고 있어야 함
    (KRX Open API가 시장별로 엔드포인트가 분리되어 있기 때문).

    반환 각 행: KRX 원본 필드 그대로(BAS_DD, ISU_CD, ISU_NM, TDD_CLSPRC 등) +
    편의를 위해 정규화된 키(date, close, open, high, low, volume, market_cap,
    shares_outstanding)를 추가로 붙인다. 필드명이 실제 API와 다를 경우
    KeyError 대신 명시적 KrxApiError로 원본 행을 노출한다.
    """
    service = SVC_KOSPI_STOCKS if market.upper() == "KOSPI" else SVC_KOSDAQ_STOCKS
    out: list[dict] = []
    for bas_dd in _business_days(start_date, end_date):
        rows = _fetch_day(service, bas_dd)
        for row in rows:
            code = row.get("ISU_CD", "")
            # ISU_CD가 6자리 단축코드인지 12자리 확장코드인지 확인되지 않았으므로
            # 둘 다 매칭 시도(끝 6자리 비교 포함).
            if code == ticker or code.endswith(ticker):
                out.append(_normalize_stock_row(row, bas_dd))
    if not out:
        raise ValueError(
            f"OHLCV 조회 결과 없음: ticker={ticker}, market={market}, "
            f"{start_date}~{end_date} (필드명 'ISU_CD' 매칭 실패 가능성 — "
            f"__main__ 진단 모드로 raw 응답을 확인하세요)"
        )
    out.sort(key=lambda r: r["date"])
    return out


def _normalize_stock_row(row: dict, bas_dd: str) -> dict:
    def _num(key: str):
        v = row.get(key)
        if v in (None, ""):
            return None
        try:
            return float(str(v).replace(",", ""))
        except ValueError:
            return None

    normalized = dict(row)
    normalized.update({
        "date": bas_dd,
        "close": _num("TDD_CLSPRC"),
        "open": _num("TDD_OPNPRC"),
        "high": _num("TDD_HGPRC"),
        "low": _num("TDD_LWPRC"),
        "volume": _num("ACC_TRDVOL"),
        "market_cap": _num("MKTCAP"),
        "shares_outstanding": _num("LIST_SHRS"),
    })
    if normalized["close"] is None:
        raise KrxApiError(
            f"필드명 불일치 가능성: 'TDD_CLSPRC' 키로 종가를 찾을 수 없습니다. "
            f"raw row={row}"
        )
    return normalized


def get_index_ohlcv(index_name: str, start_date: str, end_date: str, market: str = "KOSPI") -> list[dict]:
    """지수 일별 시세. index_name은 KRX가 반환하는 IDX_NM과 부분 일치로 매칭
    (예: '코스피', '코스피 200')."""
    service = SVC_KOSPI_INDEX if market.upper() == "KOSPI" else SVC_KOSDAQ_INDEX
    out: list[dict] = []
    for bas_dd in _business_days(start_date, end_date):
        rows = _fetch_day(service, bas_dd)
        for row in rows:
            name = row.get("IDX_NM", "")
            if index_name in name:
                out.append(_normalize_index_row(row, bas_dd))
    if not out:
        raise ValueError(
            f"지수 OHLCV 조회 결과 없음: index_name={index_name}, market={market}, "
            f"{start_date}~{end_date} (필드명 'IDX_NM' 매칭 실패 가능성)"
        )
    out.sort(key=lambda r: r["date"])
    return out


def _normalize_index_row(row: dict, bas_dd: str) -> dict:
    def _num(key: str):
        v = row.get(key)
        if v in (None, ""):
            return None
        try:
            return float(str(v).replace(",", ""))
        except ValueError:
            return None

    normalized = dict(row)
    normalized.update({
        "date": bas_dd,
        "close": _num("CLSPRC_IDX"),
        "open": _num("OPNPRC_IDX"),
        "high": _num("HGPRC_IDX"),
        "low": _num("LWPRC_IDX"),
    })
    if normalized["close"] is None:
        raise KrxApiError(
            f"필드명 불일치 가능성: 'CLSPRC_IDX' 키로 지수 종가를 찾을 수 없습니다. "
            f"raw row={row}"
        )
    return normalized


def compute_drawdown(rows: list[dict]) -> PriceWindow:
    """주어진 구간 내 최고종가 → 이후 최저종가 기준 최대 낙폭을 계산한다."""
    if not rows:
        raise ValueError("compute_drawdown: 빈 데이터")
    peak_row = max(rows, key=lambda r: r["close"])
    peak_idx = rows.index(peak_row)
    after_peak = rows[peak_idx:]
    trough_row = min(after_peak, key=lambda r: r["close"])

    dd = (trough_row["close"] - peak_row["close"]) / peak_row["close"] * 100
    return PriceWindow(
        ticker="",
        start_date=rows[0]["date"],
        end_date=rows[-1]["date"],
        peak_close=peak_row["close"],
        peak_date=peak_row["date"],
        trough_close=trough_row["close"],
        trough_date=trough_row["date"],
        drawdown_pct=float(dd),
    )


def stress_test_gate(
    stock_ticker: str,
    start_date: str,
    end_date: str,
    market: str = "KOSPI",
    index_name: str = "코스피",
) -> dict:
    """D블록 게이트: 정의된 구간에서 종목 낙폭이 지수 낙폭보다 크면 미통과.

    반환: {"index_dd": float, "stock_dd": float, "passed": bool, ...}
    passed 조건(weights.yaml 정의와 동일): 종목낙폭 >= 지수낙폭 (덜 빠지거나 상승)
    """
    idx_rows = get_index_ohlcv(index_name, start_date, end_date, market=market)
    stk_rows = get_ohlcv(stock_ticker, start_date, end_date, market=market)

    idx_window = compute_drawdown(idx_rows)
    stk_window = compute_drawdown(stk_rows)

    passed = stk_window.drawdown_pct >= idx_window.drawdown_pct  # 덜 빠지거나 상승해야 통과
    return {
        "stock_ticker": stock_ticker,
        "index_name": index_name,
        "window": f"{start_date}~{end_date}",
        "index_drawdown_pct": round(idx_window.drawdown_pct, 2),
        "stock_drawdown_pct": round(stk_window.drawdown_pct, 2),
        "index_peak_date": idx_window.peak_date,
        "index_trough_date": idx_window.trough_date,
        "stock_peak_date": stk_window.peak_date,
        "stock_trough_date": stk_window.trough_date,
        "passed": bool(passed),
    }


def get_quote(ticker: str, market: str = "KOSPI", ref_date: str | None = None) -> dict:
    """당일(또는 지정일 기준 최근 영업일) 시세 요약 — 종가/시가총액/상장주식수.

    주의: PER/PBR은 이 서비스(sto/stk_bydd_trd)에 포함되어 있지 않을 가능성이 높아
    None으로 반환한다(검증 필요 항목, 모듈 상단 docstring 참고).
    """
    if ref_date is None:
        ref_date = dt.datetime.now().strftime("%Y%m%d")

    service = SVC_KOSPI_STOCKS if market.upper() == "KOSPI" else SVC_KOSDAQ_STOCKS
    d = dt.datetime.strptime(ref_date, "%Y%m%d")
    row = None
    used_date = ref_date
    for _ in range(10):  # 최대 10 영업일 소급(연휴 등 대비)
        bas_dd = d.strftime("%Y%m%d")
        if d.weekday() < 5:
            rows = _fetch_day(service, bas_dd)
            match = next(
                (r for r in rows if r.get("ISU_CD") == ticker or r.get("ISU_CD", "").endswith(ticker)),
                None,
            )
            if match:
                row = match
                used_date = bas_dd
                break
        d -= dt.timedelta(days=1)

    if row is None:
        raise ValueError(f"시세 조회 실패: {ticker} @ {ref_date} 부터 최대 10영업일 소급 조회")

    norm = _normalize_stock_row(row, used_date)
    return {
        "ticker": ticker,
        "date": used_date,
        "close": norm["close"],
        "market_cap_krw": norm["market_cap"],
        "shares_outstanding": norm["shares_outstanding"],
        "per": None,  # TODO: 정확한 투자지표 서비스 경로 확인 후 채우기 (미검증)
        "pbr": None,  # TODO: 상동
    }


def get_market_snapshot(market: str = "KOSPI", ref_date: str | None = None, max_lookback_days: int = 10) -> tuple[list[dict], str]:
    """지정 시장의 특정 영업일자 전종목 스냅샷(종목코드/종목명/종가/시가총액)을 반환한다.

    2026-09-28 배경(중요, 리오님과 확정): KOSPI200/KOSDAQ150 "공식 지수 구성종목"은
    openapi.krx.co.kr(이 모듈이 쓰는 공식 키 기반 API)에 조회 엔드포인트 자체가 없고,
    실제로는 data.krx.co.kr(정보데이터시스템)의 비공식 내부 API에서만 제공되는데
    이 경로는 2024-12-27 이후 로그인(KRX_ID/PW)이 의무화됐다(봇탐지 목적). 일일
    GitHub Actions 자동 실행에서 개인 계정 로그인을 매일 자동화하면 KRX 봇탐지
    정책에 걸려 계정이 제한/정지될 위험이 있어(모듈 상단 히스토리와 동일한 이유로
    이 아키텍처를 피해왔음), 정확한 지수 구성종목 대신 "시가총액 상위 N종목"을
    대리 지표(proxy)로 쓰기로 리오님과 합의했다(2026-09-28). 이 함수와
    get_top_n_by_market_cap()이 그 대리 지표를 만든다 — 이미 승인된 공식 API만
    쓰므로 추가 키나 로그인이 전혀 필요 없다.

    ref_date가 None이면 오늘부터 최대 max_lookback_days 영업일 소급하며 데이터가
    있는 가장 최근 날짜를 사용한다(휴장일/당일 미정산 대응, get_quote()와 동일 패턴).

    반환: (rows, used_date). rows의 각 행은 {"ticker", "name", "close",
    "market_cap", "shares_outstanding"}.
    """
    service = SVC_KOSPI_STOCKS if market.upper() == "KOSPI" else SVC_KOSDAQ_STOCKS
    d = dt.datetime.now() if ref_date is None else dt.datetime.strptime(ref_date, "%Y%m%d")

    for _ in range(max_lookback_days):
        if d.weekday() < 5:
            bas_dd = d.strftime("%Y%m%d")
            raw_rows = _fetch_day(service, bas_dd)
            if raw_rows:
                rows = []
                for r in raw_rows:
                    norm = _normalize_stock_row(r, bas_dd)
                    rows.append({
                        "ticker": r.get("ISU_CD", "")[-6:],  # 12자리 확장코드일 경우 끝 6자리가 단축코드
                        "name": r.get("ISU_NM") or r.get("ISU_ABBRV") or "",
                        "close": norm["close"],
                        "market_cap": norm["market_cap"],
                        "shares_outstanding": norm["shares_outstanding"],
                    })
                return rows, bas_dd
        d -= dt.timedelta(days=1)

    raise ValueError(
        f"시장 스냅샷 조회 실패: market={market}, {max_lookback_days}영업일 소급 조회에도 데이터 없음"
    )


def get_top_n_by_market_cap(market: str = "KOSPI", n: int = 200, ref_date: str | None = None) -> list[dict]:
    """get_market_snapshot() 결과를 시가총액(market_cap) 내림차순 정렬해 상위 n개만 반환한다.
    market_cap이 None인 행(데이터 결측)은 순위에서 제외한다(지어내지 않음)."""
    rows, used_date = get_market_snapshot(market=market, ref_date=ref_date)
    ranked = sorted(
        (r for r in rows if r.get("market_cap") is not None),
        key=lambda r: r["market_cap"],
        reverse=True,
    )
    top = ranked[:n]
    for r in top:
        r["as_of"] = used_date
    return top


def detect_share_jumps(
    rows: list[dict], threshold_high: float = 1.5, threshold_low: float = 0.667
) -> list[dict]:
    """상장주식수(LIST_SHRS)의 전일 대비 급변(액면분할/합병/유상증자 등으로 인한
    수정주가 불연속 가능성)을 탐지한다. 커뮤니티 예제(krx-openapi-examples)의
    share_jumps() 휴리스틱을 동일하게 적용: 전일 대비 1.5배 이상 증가 또는
    0.667배 이하 감소 시 플래그.

    이 모듈이 제공하는 종가는 KRX Open API 원본(수정주가 미적용)이므로,
    장기 시계열을 이어붙여 쓸 경우 반드시 이 함수로 불연속 구간을 먼저 확인해야 한다.
    """
    jumps = []
    for i in range(1, len(rows)):
        prev = rows[i - 1].get("shares_outstanding")
        curr = rows[i].get("shares_outstanding")
        if not prev or not curr:
            continue
        ratio = curr / prev
        if ratio >= threshold_high or ratio <= threshold_low:
            jumps.append({
                "date": rows[i]["date"],
                "prev_shares": prev,
                "curr_shares": curr,
                "ratio": round(ratio, 3),
            })
    return jumps


if __name__ == "__main__":
    import json

    print("=== KRX Open API 연결성 진단 (AUTH_KEY 없이도 실행 가능) ===")
    try:
        rows = _fetch_day(SVC_KOSPI_STOCKS, dt.datetime.now().strftime("%Y%m%d"))
        print(f"예상치 못하게 성공: {len(rows)}건 (AUTH_KEY가 설정되어 있고 승인된 상태)")
        if rows:
            print("첫 행 raw 필드(실제 필드명 확인용):")
            print(json.dumps(rows[0], ensure_ascii=False, indent=2))
    except KrxApiError as e:
        print(f"예상된 오류(정상): {e}")

    if os.environ.get("KRX_AUTH_KEY"):
        print("\n=== 라이브 테스트: 삼성전자(005930) D블록 스트레스테스트 게이트 ===")
        try:
            result = stress_test_gate("005930", "20260601", "20260831", market="KOSPI", index_name="코스피")
            print(json.dumps(result, ensure_ascii=False, indent=2))

            print("\n=== 라이브 테스트: 삼성전자 현재 시세 ===")
            q = get_quote("005930", market="KOSPI")
            print(json.dumps(q, ensure_ascii=False, indent=2))
        except (KrxApiError, ValueError) as e:
            print(f"라이브 테스트 실패 — 필드명 검증 필요: {e}")
    else:
        print(
            "\nKRX_AUTH_KEY 미설정 — 라이브 테스트는 건너뜁니다. "
            "openapi.krx.co.kr에서 키 발급 후 export KRX_AUTH_KEY=... 로 재실행하세요."
        )
