"""
dart_collector.py
OpenDART(금융감독원 전자공시시스템 Open API) 기반 한국 기업 재무 데이터 수집 모듈.

=== 사용 전 필수 준비 ===
1. https://opendart.fss.or.kr 에서 무료 회원가입 후 인증키(crtfc_key) 발급
2. 환경변수 DART_API_KEY 로 설정

=== 검증 상태 (2026-09-28) ===
- 엔드포인트 리처빌리티: curl로 직접 확인 완료.
  - fnlttSinglAcnt.json (재무제표) → HTTP 200 + {"status":"010","message":"등록되지
    않은 인증키입니다."} (유효하지 않은 키에 대한 DART 표준 오류 포맷 확인)
  - corpCode.xml (전종목 고유번호 매핑, 대량다운로드) → 유효하지 않은 키일 때
    XML 상태 래퍼({"status":"010",...})를 그대로 반환, 유효한 키일 때는 ZIP 바이너리
    (CORPCODE.xml 포함)를 반환하는 것으로 공식 문서상 알려져 있음 — 이 모듈은 응답이
    ZIP인지 아닌지로 분기 처리한다.
- 필드명/enum: 이 세션에서 PlayMCP(mcp__PlayMCP__opendart-*) 도구로 삼성전자/KT&G
  데이터를 직접 조회하며 실측 검증한 값들을 그대로 사용:
  - idx_cl_code: M210000=수익성지표, M220000=안정성지표, M230000=성장성지표,
    M240000=활동성지표
  - reprt_code: 11011=사업보고서(연간), 11012=반기보고서, 11013=1분기보고서,
    11014=3분기보고서
  - fnlttSinglAcntAll(전체 재무제표)의 현금흐름표 계정명: "영업활동현금흐름"(OCF),
    "유형자산의 취득", "무형자산의 취득" (CapEx 구성요소) — 삼성전자 실제 공시에서
    이 문자열로 매칭 성공 확인(예: FY2025 영업활동현금흐름=85,315,148백만원).
  - status 코드(DART 공식 문서 + 이 세션에서 실제로 마주친 코드): 000=정상,
    010=등록되지않은키, 011=사용할수없는키(비활성화), 012=접근할수없는IP,
    013=조회된데이터없음(정상적인 '데이터 없음' — 예외로 취급하지 않음),
    020=요청제한초과, 100=필드누락/형식오류, 800=시스템점검중, 900/901=기타오류.
- 미검증: 이 모듈 자체(requests 기반 순수 Python)로 실제 유효한 DART_API_KEY를 이용한
  라이브 호출은 아직 수행하지 못함(이 세션은 DART_API_KEY를 보유하고 있지 않음 —
  PlayMCP는 별도 백엔드 인증을 쓰는 도구이며 이 스크립트의 crtfc_key와는 무관).
  사용자가 DART_API_KEY를 제공하면 __main__ 진단 모드로 1회 실행해 검증 필요.
"""
from __future__ import annotations

import io
import os
import time
import zipfile
from pathlib import Path

import requests

BASE_URL = "https://opendart.fss.or.kr/api/{}"
CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "cache"

# reprt_code
REPRT_ANNUAL = "11011"       # 사업보고서
REPRT_HALF = "11012"         # 반기보고서
REPRT_Q1 = "11013"           # 1분기보고서
REPRT_Q3 = "11014"           # 3분기보고서

# idx_cl_code (재무지표 구분)
IDX_PROFITABILITY = "M210000"  # 수익성지표 (ROE, 영업이익률 등)
IDX_STABILITY = "M220000"      # 안정성지표 (부채비율 등)
IDX_GROWTH = "M230000"         # 성장성지표
IDX_ACTIVITY = "M240000"       # 활동성지표

_STATUS_MEANINGS = {
    "000": "정상",
    "010": "등록되지 않은 인증키",
    "011": "사용할 수 없는 인증키(비활성화)",
    "012": "접근할 수 없는 IP",
    "013": "조회된 데이터가 없습니다",  # 정상적인 '없음' — 예외 아님
    "014": "파일이 존재하지 않습니다",
    "020": "요청 제한을 초과하였습니다",
    "100": "필드의 부적절한 값입니다",
    "101": "부적절한 접근입니다",
    "800": "시스템 점검중입니다",
    "900": "정의되지 않은 오류가 발생하였습니다",
    "901": "사용자 계정의 개인정보 보유기간이 만료되었습니다",
}


class DartApiError(Exception):
    """DART Open API 관련 오류(키 미설정, status!=000, 예상치 못한 응답 구조 등)."""


class DartNoDataError(DartApiError):
    """status=013(조회된 데이터가 없습니다) — 정상적인 '해당 연도/구분 데이터 없음'.
    호출자가 다른 연도/다른 endpoint로 재시도할 수 있도록 별도 예외로 분리."""


def _api_key() -> str:
    key = os.environ.get("DART_API_KEY")
    if not key:
        raise DartApiError(
            "DART_API_KEY 환경변수가 설정되지 않았습니다. "
            "https://opendart.fss.or.kr 에서 무료 발급 후 설정하세요."
        )
    return key


def _get_json(endpoint: str, params: dict, max_retries: int = 2) -> dict:
    url = BASE_URL.format(endpoint)
    req_params = {"crtfc_key": _api_key(), **params}

    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.get(url, params=req_params, timeout=20)
        except requests.RequestException as e:
            last_err = e
            time.sleep(1)
            continue

        if resp.status_code != 200:
            raise DartApiError(f"DART API HTTP 오류 {resp.status_code}: {resp.text[:300]}")

        try:
            payload = resp.json()
        except ValueError as e:
            raise DartApiError(
                f"DART API 응답이 JSON이 아닙니다(endpoint={endpoint}): {resp.text[:300]}"
            ) from e

        status = payload.get("status")
        if status == "013":
            raise DartNoDataError(
                f"{_STATUS_MEANINGS.get(status, status)} (endpoint={endpoint}, params={params})"
            )
        if status != "000":
            raise DartApiError(
                f"DART API 오류 status={status} "
                f"({_STATUS_MEANINGS.get(status, '알 수 없는 코드')}): "
                f"{payload.get('message', '')} (endpoint={endpoint}, params={params})"
            )
        return payload

    raise DartApiError(f"네트워크 오류로 {max_retries + 1}회 시도 후 실패: {last_err}")


# 2026-09-29: 850종목 규모 1차 실전 실행 2회 모두 이 함수(corpCode.xml 대량
# 다운로드)가 opendart.fss.or.kr에 대한 커넥션 타임아웃으로 실패했는데, 이
# 함수에 실패를 기억하는 장치가 없어서 KR 후보 350개가 "각자" 이 다운로드를
# 재시도하며 종목당 250~300초씩(관측값) 날리다 GitHub Actions 90분
# 타임아웃으로 전체 파이프라인이 강제취소되는 사고가 실제로 2회 발생했다
# (실행 ID 36506472350, 36514245359 — 20종목도 채 못 끝냄, 예상 잔여시간이
# 24시간 이상으로 찍힘). 프로세스 1회 실행 중 이미 실패했으면 같은 오류를
# 즉시(네트워크 재시도 없이) 재발생시켜 나머지 후보들이 수 초 내로 스킵되게
# 하는 프로세스 내 서킷브레이커를 추가한다.
_corp_code_map_memo: dict[str, str] | None = None
_corp_code_map_memo_error: Exception | None = None


def download_corp_code_map(force_refresh: bool = False) -> dict[str, str]:
    """전종목(상장+비상장 포함) 고유번호(corp_code) ↔ 종목코드(stock_code) 매핑을
    다운로드하고 로컬에 캐시한다. DART는 이 전체 목록을 ZIP(내부에 CORPCODE.xml)
    형태로만 제공하므로, 매번 호출하지 말고 캐시를 우선 사용해야 한다(용량이 큼).

    반환: {stock_code(6자리): corp_code(8자리)} — stock_code가 빈 값인 비상장/
    코드없는 법인은 제외하고 반환한다.
    """
    global _corp_code_map_memo, _corp_code_map_memo_error

    if not force_refresh and _corp_code_map_memo is not None:
        return _corp_code_map_memo
    if not force_refresh and _corp_code_map_memo_error is not None:
        raise _corp_code_map_memo_error

    cache_file = CACHE_DIR / "corp_code_map.json"
    cache_age_days: float | None = None
    if cache_file.exists():
        cache_age_days = (time.time() - cache_file.stat().st_mtime) / 86400
        # corp_code 매핑은 거의 안 바뀌므로(신규상장 정도) 25일 이내면
        # 그냥 재사용 — GitHub Actions 캐시(actions/cache, 워크플로 참고)로
        # 실행 간에도 영속화되므로 평소엔 네트워크 호출 자체가 없다.
        if not force_refresh and cache_age_days <= 25:
            import json
            _corp_code_map_memo = json.loads(cache_file.read_text(encoding="utf-8"))
            return _corp_code_map_memo

    def _load_stale_cache_fallback(reason: Exception) -> dict[str, str] | None:
        if cache_file.exists():
            print(
                f"[dart_collector] corpCode.xml 갱신 실패, {cache_age_days:.0f}일 지난 "
                f"캐시로 폴백(corp_code 매핑은 자주 안 바뀌므로 완전 실패보단 나음): {reason}"
            )
            import json
            return json.loads(cache_file.read_text(encoding="utf-8"))
        return None

    url = BASE_URL.format("corpCode.xml")
    last_err: Exception | None = None
    resp = None
    # 커넥션 타임아웃(60s 설정에도 실측 250~300초 소요 관측 — DNS/IPv6 폴백
    # 등 OS 레벨 지연으로 추정)이 원인이라 타임아웃을 (연결10s, 응답30s)로
    # 짧게 잡고, 대신 2회만 빠르게 재시도한다.
    for attempt in range(2):
        try:
            resp = requests.get(url, params={"crtfc_key": _api_key()}, timeout=(10, 30))
            break
        except requests.RequestException as e:
            last_err = e
            resp = None
            if attempt == 0:
                time.sleep(3)

    if resp is None:
        fallback = _load_stale_cache_fallback(last_err)
        if fallback is not None:
            _corp_code_map_memo = fallback
            return fallback
        err = DartApiError(f"corpCode.xml 다운로드 실패(네트워크, 2회 시도): {last_err}")
        _corp_code_map_memo_error = err
        raise err

    if resp.status_code != 200:
        e = DartApiError(f"corpCode.xml HTTP 오류 {resp.status_code}: {resp.text[:300]}")
        fallback = _load_stale_cache_fallback(e)
        if fallback is not None:
            _corp_code_map_memo = fallback
            return fallback
        _corp_code_map_memo_error = e
        raise e

    content = resp.content
    if not zipfile.is_zipfile(io.BytesIO(content)):
        # 유효한 키가 아니면 ZIP이 아니라 status XML 래퍼가 그대로 옴
        e = DartApiError(
            f"corpCode.xml 응답이 ZIP이 아닙니다(키 오류 가능성): {content[:300]!r}"
        )
        fallback = _load_stale_cache_fallback(e)
        if fallback is not None:
            _corp_code_map_memo = fallback
            return fallback
        _corp_code_map_memo_error = e
        raise e

    import xml.etree.ElementTree as ET
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        xml_name = next(n for n in zf.namelist() if n.upper().endswith(".XML"))
        xml_bytes = zf.read(xml_name)

    root = ET.fromstring(xml_bytes)
    mapping: dict[str, str] = {}
    for item in root.iter("list"):
        stock_code = (item.findtext("stock_code") or "").strip()
        corp_code = (item.findtext("corp_code") or "").strip()
        if stock_code:
            mapping[stock_code] = corp_code

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    import json
    cache_file.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    _corp_code_map_memo = mapping
    _corp_code_map_memo_error = None
    return mapping


def find_corp_code(stock_code: str) -> str:
    """종목코드(예: '005930')로 DART corp_code(예: '00126380')를 찾는다."""
    mapping = download_corp_code_map()
    corp_code = mapping.get(stock_code)
    if not corp_code:
        raise DartApiError(f"corp_code를 찾을 수 없습니다: stock_code={stock_code}")
    return corp_code


def get_financial_account(
    corp_code: str, bsns_year: str, reprt_code: str = REPRT_ANNUAL, fs_div: str = "CFS"
) -> list[dict]:
    """단일회사 주요계정(fnlttSinglAcnt.json) — 자산/부채/자본/매출/이익잉여금 등
    재무제표 핵심 계정. fs_div: CFS=연결재무제표, OFS=개별재무제표."""
    payload = _get_json(
        "fnlttSinglAcnt.json",
        {"corp_code": corp_code, "bsns_year": bsns_year, "reprt_code": reprt_code},
    )
    return payload.get("list", [])


def get_financial_index(
    corp_code: str, bsns_year: str, reprt_code: str = REPRT_ANNUAL,
    idx_cl_code: str = IDX_PROFITABILITY,
) -> list[dict]:
    """단일회사 주요 재무지표(fnlttSinglIndx.json) — ROE, 부채비율 등 이미 계산된
    비율. 일부 연도/idx_cl_code 조합은 013(데이터없음)이 정상적으로 발생할 수 있음
    (이 세션에서 실제로 확인된 패턴) — 이 경우 get_financial_account으로 원시
    금액을 받아 직접 계산하는 것으로 대체해야 한다."""
    payload = _get_json(
        "fnlttSinglIndx.json",
        {
            "corp_code": corp_code,
            "bsns_year": bsns_year,
            "reprt_code": reprt_code,
            "idx_cl_code": idx_cl_code,
        },
    )
    return payload.get("list", [])


def get_full_financial_statement(
    corp_code: str, bsns_year: str, reprt_code: str = REPRT_ANNUAL, fs_div: str = "CFS"
) -> list[dict]:
    """전체 재무제표(fnlttSinglAcntAll.json) — 재무상태표/손익계산서/현금흐름표
    전체 계정. 응답이 매우 커질 수 있음(이 세션에서 삼성전자 기준 약 13만자).
    OCF/CapEx 추출은 get_owner_earnings()를 사용."""
    payload = _get_json(
        "fnlttSinglAcntAll.json",
        {
            "corp_code": corp_code,
            "bsns_year": bsns_year,
            "reprt_code": reprt_code,
            "fs_div": fs_div,
        },
    )
    return payload.get("list", [])


def get_dividend_info(corp_code: str, bsns_year: str, reprt_code: str = REPRT_ANNUAL) -> list[dict]:
    """배당에 관한 사항(alotMatter.json) — 주당 배당금, 배당성향, 배당수익률 등."""
    payload = _get_json(
        "alotMatter.json",
        {"corp_code": corp_code, "bsns_year": bsns_year, "reprt_code": reprt_code},
    )
    return payload.get("list", [])


def get_treasury_stock(corp_code: str, bsns_year: str, reprt_code: str = REPRT_ANNUAL) -> list[dict]:
    """자기주식 취득 및 처분 현황(tesstkAcqsDspsSttus.json) — 자사주 매입/소각.

    2026-09-29 실측 중 발견: 엔드포인트명에 오타("tesstkAcqusDspsSttus" — Acqus)가
    있어 DART가 status=101(잘못된 URL) 오류를 냈다. 공식 개발가이드
    (opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS002&apiId=2019006)로 정확한
    이름("tesstkAcqsDspsSttus" — Acqs)을 확인해 수정."""
    payload = _get_json(
        "tesstkAcqsDspsSttus.json",
        {"corp_code": corp_code, "bsns_year": bsns_year, "reprt_code": reprt_code},
    )
    return payload.get("list", [])


def _to_number(v: str | None) -> float | None:
    if v in (None, "", "-"):
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def get_owner_earnings(
    corp_code: str, bsns_year: str, reprt_code: str = REPRT_ANNUAL, fs_div: str = "CFS"
) -> dict:
    """margin_of_safety_gate(weights.yaml)에 필요한 Owner Earnings = OCF - CapEx를
    전체 재무제표 현금흐름표에서 계정명 매칭으로 직접 추출한다.

    이 세션에서 삼성전자로 실측 검증한 계정명 매칭 패턴을 그대로 사용:
    - 영업활동현금흐름 → account_nm에 "영업활동" and "현금흐름" 포함, sj_div=="CF"
    - 유형자산의 취득 → account_nm에 "유형자산" and "취득" 포함
    - 무형자산의 취득 → account_nm에 "무형자산" and "취득" 포함
    """
    rows = get_full_financial_statement(corp_code, bsns_year, reprt_code, fs_div)
    cf_rows = [r for r in rows if r.get("sj_div") == "CF"]

    def _find(keywords: list[str]) -> float | None:
        for r in cf_rows:
            name = r.get("account_nm", "")
            if all(k in name for k in keywords):
                val = _to_number(r.get("thstrm_amount"))
                if val is not None:
                    return val
        return None

    ocf = _find(["영업활동", "현금흐름"])
    capex_tangible = _find(["유형자산", "취득"])
    capex_intangible = _find(["무형자산", "취득"])

    if ocf is None:
        raise DartApiError(
            f"영업활동현금흐름 계정을 찾지 못했습니다(corp_code={corp_code}, "
            f"bsns_year={bsns_year}) — 계정명 표기가 공시마다 다를 수 있어 재확인 필요. "
            f"CF 섹션 계정명 목록: {[r.get('account_nm') for r in cf_rows][:30]}"
        )

    capex = (capex_tangible or 0) + (capex_intangible or 0)
    return {
        "corp_code": corp_code,
        "bsns_year": bsns_year,
        "ocf": ocf,
        "capex_tangible": capex_tangible,
        "capex_intangible": capex_intangible,
        "capex_total": capex,
        "owner_earnings": ocf - capex,
        "unit": "원 (DART 보고서 단위 그대로 — 보통 원 단위, 일부는 백만원 표기 상이하니 amount 값 자체와 report 단위 표기를 함께 확인할 것)",
    }


if __name__ == "__main__":
    import json

    print("=== DART Open API 연결성 진단 (DART_API_KEY 없이도 부분 실행 가능) ===")
    try:
        payload = requests.get(
            BASE_URL.format("fnlttSinglAcnt.json"),
            params={
                "crtfc_key": os.environ.get("DART_API_KEY", "TEST_INVALID_KEY"),
                "corp_code": "00126380",  # 삼성전자
                "bsns_year": "2025",
                "reprt_code": REPRT_ANNUAL,
            },
            timeout=15,
        ).json()
        print(json.dumps(payload, ensure_ascii=False, indent=2)[:1000])
    except Exception as e:  # noqa: BLE001 - 진단 목적
        print(f"진단 호출 자체 실패(네트워크 문제 가능성): {e}")

    if os.environ.get("DART_API_KEY"):
        print("\n=== 라이브 테스트: 삼성전자(005930) corp_code 조회 ===")
        try:
            cc = find_corp_code("005930")
            print(f"corp_code = {cc}")

            print("\n=== 라이브 테스트: Owner Earnings (FY2025) ===")
            oe = get_owner_earnings(cc, "2025")
            print(json.dumps(oe, ensure_ascii=False, indent=2))
        except DartApiError as e:
            print(f"라이브 테스트 실패: {e}")
    else:
        print(
            "\nDART_API_KEY 미설정 — 라이브 테스트는 건너뜁니다. "
            "https://opendart.fss.or.kr 에서 키 발급 후 export DART_API_KEY=... 로 재실행하세요."
        )
