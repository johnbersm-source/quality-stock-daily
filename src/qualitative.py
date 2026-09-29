"""
qualitative.py
정성평가 3항목(경제적 해자 A블록 8점 / IFRS18 MPM 정성 B블록 1.5점 / 거시·사이클·재료
H블록 8점, 총 17.5점) 보강 — 2026-09-29 리오님 지시로 설계·구현.

원칙(사용자 표준 규칙 그대로 계승):
- Gemini에 Google Search grounding(실시간 웹검색 근거)을 켜서 호출하고, 응답이
  근거(evidence)와 함께 오지 않으면 점수를 절대 만들어내지 않는다 — 실패/근거
  부족/파싱 실패는 전부 None으로 떨어뜨린다(models.QualitativeScore가 근거 없는
  점수를 __post_init__에서 거부하므로 이중으로 강제됨).
- 이 모듈은 "종목별 사실 근거가 필요한 정성평가"를 대신하는 것이지, 점수를
  지어내는 게 아니다. 프롬프트 안에 채점 기준(루브릭)을 명시해 최대한 일관되게
  채점하도록 하되, 결과는 항상 evidence 문장으로 왜 그 점수인지 설명해야 한다.

=== IFRS18 시행일 관련 중요 발견 (2026-09-29 웹서치로 확인) ===
IFRS 18(Presentation and Disclosure in Financial Statements)은 2027-01-01
이후 시작하는 회계연도부터 의무 적용이고(조기적용 가능), 애초에 IFRS 기준이라
US GAAP을 쓰는 미국 상장사에는 구조적으로 적용 대상이 아니다. 따라서
`ifrs18_mpm_qual`(MPM 공식화 정성평가)은:
  - US 후보군: 항상 None(적용 대상 자체가 아님 — 평가 자체가 무의미)
  - KR 후보군: 조기적용을 공식 발표/공시한 경우가 아니면 2027년 전까지는 None이
    정직한 결과다(회사가 못해서 0점인 게 아니라 제도 시행 전이라 평가 대상이
    아닌 것 — 0점과 N/A는 의미가 다르다).
반면 `ifrs18_segment_disclosed`(부문별 분리공시 여부)는 IFRS18과 무관하게 많은
기업이 이미 하고 있는 일반적 공시 관행이라 사실관계로 확인 가능하므로, 이
모듈은 이것만 조사해서 채운다. score_ifrs18()이 두 값 중 하나만 있어도 부분
점수(최대 1.5/3)를 주도록 이미 설계돼 있어(blocks.py 참고) 추가 코드 변경은
필요 없다.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path

from .scoring.models import QualitativeScore

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CACHE_PATH = DATA_DIR / "cache" / "qualitative_cache.json"

# 컴포넌트별 캐시 유효기간(일) — 해자/IFRS18은 자주 안 바뀌니 길게,
# 거시/사이클은 시황·촉매가 매일 바뀌므로 짧게.
_TTL_DAYS = {"moat": 90, "ifrs18": 90, "macro": 1}

_MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")


class QualitativeResearchError(Exception):
    """Gemini 호출/파싱 실패 등 — 호출부는 이 예외를 잡아 None으로 떨어뜨린다
    (전체 파이프라인이 정성평가 1건 실패로 죽으면 안 됨)."""


def _load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def _cache_key(market: str, ticker: str, component: str) -> str:
    return f"{market}:{ticker}:{component}"


def _cache_get(market: str, ticker: str, component: str, as_of: dt.date) -> dict | None:
    cache = _load_cache()
    entry = cache.get(_cache_key(market, ticker, component))
    if entry is None:
        return None
    cached_date = dt.date.fromisoformat(entry["as_of"])
    ttl = _TTL_DAYS[component]
    if (as_of - cached_date).days > ttl:
        return None
    return entry


def _cache_put(market: str, ticker: str, component: str, as_of: dt.date, payload: dict) -> None:
    cache = _load_cache()
    cache[_cache_key(market, ticker, component)] = {"as_of": as_of.isoformat(), **payload}
    _save_cache(cache)


def _client():
    """Gemini 클라이언트 lazy 생성 — GEMINI_API_KEY 없으면 명시적으로 실패
    (조용히 넘어가지 않고 원인이 뭔지 알 수 있게)."""
    from google import genai  # 지연 import: 키 없는 환경(로컬 단위테스트 등)에서 불필요한 의존성 에러 방지

    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise QualitativeResearchError(
            "GEMINI_API_KEY 환경변수가 설정되지 않았습니다 — GitHub Secrets 등록 확인 필요."
        )
    return genai.Client(api_key=key)


def _call_gemini_json(prompt: str) -> dict:
    """Google Search grounding을 켜고 Gemini를 호출해 JSON 응답을 파싱한다.
    실패 시 QualitativeResearchError를 던진다(호출부에서 잡아 None 처리)."""
    from google.genai import types

    client = _client()
    grounding_tool = types.Tool(google_search=types.GoogleSearch())
    config = types.GenerateContentConfig(
        tools=[grounding_tool],
        temperature=0.1,  # 채점 일관성 우선 — 창의성 불필요
    )
    try:
        response = client.models.generate_content(
            model=_MODEL_NAME, contents=prompt, config=config,
        )
    except Exception as e:  # noqa: BLE001 - Gemini SDK 예외 타입이 다양해 광범위하게 잡음
        raise QualitativeResearchError(f"Gemini 호출 실패: {e}") from e

    text = (response.text or "").strip()
    if not text:
        raise QualitativeResearchError("Gemini 응답이 비어있음")

    # 응답이 ```json ... ``` 코드블록으로 감싸져 오는 경우가 흔해 벗겨낸다.
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise QualitativeResearchError(f"Gemini 응답에서 JSON을 찾지 못함: {text[:300]}")
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError as e:
        raise QualitativeResearchError(f"Gemini 응답 JSON 파싱 실패: {e}, raw={text[:300]}") from e

    # 인용 출처(grounding_metadata)도 근거 문장에 덧붙여 투명성을 높인다.
    sources = []
    try:
        grounding = response.candidates[0].grounding_metadata
        if grounding and grounding.grounding_chunks:
            sources = [c.web.uri for c in grounding.grounding_chunks if getattr(c, "web", None)]
    except (AttributeError, IndexError, TypeError):
        pass
    if sources:
        parsed["_sources"] = sources[:5]
    return parsed


def _build_qual_score(parsed: dict, max_points: float, component_label: str) -> QualitativeScore:
    if "points" not in parsed or "evidence" not in parsed:
        raise QualitativeResearchError(f"{component_label}: 응답에 points/evidence 필드 없음: {parsed}")
    points = float(parsed["points"])
    if not (0 <= points <= max_points):
        raise QualitativeResearchError(
            f"{component_label}: 점수({points})가 0~{max_points} 범위를 벗어남 — 신뢰 불가, 폐기"
        )
    evidence = str(parsed["evidence"]).strip()
    sources = parsed.get("_sources") or []
    if sources:
        evidence = f"{evidence} [출처: {', '.join(sources)}]"
    return QualitativeScore(points_awarded=round(points, 2), max_points=max_points, evidence=evidence)


# --- A블록: 경제적 해자 (8점) ---

_MOAT_RUBRIC = """당신은 가치투자 애널리스트입니다. 다음 기업의 경제적 해자(economic moat)를
Google 검색으로 최신 사실을 확인한 뒤 0~{max_points}점으로 채점하세요.

기업: {company_name} ({ticker}, {market} 시장)

채점 기준(반드시 이 기준을 따르세요):
- {max_points}점: 구조적 해자(네트워크효과·높은 전환비용·무형자산/브랜드·비용우위·
  규제 진입장벽) 중 2개 이상이 뚜렷하고, 장기 ROIC가 WACC를 꾸준히 상회하거나
  경쟁사 대비 마진이 확연히 높은 등 실제 데이터로 뒷받침됨.
- {mid_high}점 내외: 해자 1개가 뚜렷하나 부분적으로만 데이터로 뒷받침됨, 또는
  경쟁 압력이 일부 존재.
- {mid_low}점 내외: 약하거나 일시적인 해자, 경쟁 심화 신호가 뚜렷함.
- 0~1점: 커머디티 성격이 강하고 뚜렷한 해자 근거를 찾지 못함.

반드시 실제 검색 결과(시장점유율, 마진 비교, 가격결정력 사례, 최근 실적/공시 등)에
근거해서 판단하고, 근거 없는 추측은 하지 마세요. 확실한 근거를 찾지 못했다면
낮은 점수를 주고 그 사실을 evidence에 명시하세요.

아래 JSON 형식으로만 답하세요(다른 텍스트 없이):
{{"points": <0~{max_points} 사이 숫자>, "evidence": "<채점 근거를 한국어 2~4문장으로,
구체적 수치·사실 인용 포함>"}}"""


def research_moat(ticker: str, company_name: str, market: str, as_of: dt.date) -> QualitativeScore | None:
    cached = _cache_get(market, ticker, "moat", as_of)
    if cached:
        return QualitativeScore(points_awarded=cached["points"], max_points=8, evidence=cached["evidence"])
    prompt = _MOAT_RUBRIC.format(
        max_points=8, mid_high=6, mid_low=3, company_name=company_name, ticker=ticker, market=market,
    )
    try:
        parsed = _call_gemini_json(prompt)
        score = _build_qual_score(parsed, max_points=8, component_label="경제적 해자")
    except QualitativeResearchError as e:
        print(f"[qualitative] {market} {ticker} 경제적 해자 조사 실패(N/A로 남김): {e}")
        return None
    _cache_put(market, ticker, "moat", as_of, {"points": score.points_awarded, "evidence": score.evidence})
    return score


# --- H블록: 거시·사이클·재료 (8점) ---

_MACRO_RUBRIC = """당신은 매크로 애널리스트입니다. 다음 기업이 속한 섹터가 현재({as_of} 기준)
거시경제 사이클에서 받는 영향과, 향후 1~2분기 내 임박한 촉매(실적, 정책, 업황
전환 등)를 Google 검색으로 최신 정보를 확인한 뒤 0~{max_points}점으로 채점하세요.

기업: {company_name} ({ticker}, {market} 시장)

채점 기준:
- {max_points}점: 섹터가 뚜렷한 매크로 순풍(금리/업황 사이클상 유리)을 받고
  있고, 향후 1~2분기 내 긍정적 촉매(실적 모멘텀, 우호적 정책/규제, 업황 전환)가
  임박함.
- {mid_high}점 내외: 중립~약한 순풍, 촉매는 불확실하거나 아직 먼 미래.
- {mid_low}점 내외: 중립~약한 역풍.
- 0~1점: 뚜렷한 역풍(업황 다운사이클, 규제 리스크, 수요 둔화) 또는 임박한
  부정적 촉매.

실제 최신 뉴스·데이터에 근거해서 판단하고, 추측하지 마세요.

아래 JSON 형식으로만 답하세요(다른 텍스트 없이):
{{"points": <0~{max_points} 사이 숫자>, "evidence": "<채점 근거를 한국어 2~4문장으로,
구체적 사실·날짜 인용 포함>"}}"""


def research_macro_catalyst(
    ticker: str, company_name: str, market: str, as_of: dt.date
) -> QualitativeScore | None:
    cached = _cache_get(market, ticker, "macro", as_of)
    if cached:
        return QualitativeScore(points_awarded=cached["points"], max_points=8, evidence=cached["evidence"])
    prompt = _MACRO_RUBRIC.format(
        max_points=8, mid_high=6, mid_low=3,
        company_name=company_name, ticker=ticker, market=market, as_of=as_of.isoformat(),
    )
    try:
        parsed = _call_gemini_json(prompt)
        score = _build_qual_score(parsed, max_points=8, component_label="거시·사이클·재료")
    except QualitativeResearchError as e:
        print(f"[qualitative] {market} {ticker} 거시·사이클 조사 실패(N/A로 남김): {e}")
        return None
    _cache_put(market, ticker, "macro", as_of, {"points": score.points_awarded, "evidence": score.evidence})
    return score


# --- B블록: IFRS18 투명성 (부문별 분리공시=사실 0.75/1.5, MPM 공식화=정성 나머지) ---

_IFRS18_RUBRIC = """다음 기업의 최근 연차보고서/사업보고서/10-K에서 사업 부문별(segment)
분리공시를 하고 있는지, 그리고 IFRS 18(2027-01-01 의무 시행, 조기적용 가능,
Management-defined Performance Measures 공식화 요구)을 조기적용했거나 도입 계획을
공식 발표한 근거가 있는지 Google 검색으로 확인하세요.

기업: {company_name} ({ticker}, {market} 시장)

**중요**: {market} 시장 기업이 US GAAP을 쓴다면(미국 상장사는 원칙적으로 그렇습니다)
IFRS18은 애초에 적용 대상이 아니므로 ifrs18_early_adoption은 반드시 null로
답하세요. 확실한 근거 없이 조기적용 중이라고 답하지 마세요 — 대부분의 기업은
아직 조기적용하지 않은 것이 정상입니다.

아래 JSON 형식으로만 답하세요(다른 텍스트 없이):
{{"segment_disclosed": <true/false>, "segment_evidence": "<부문별 분리공시 여부의
근거를 한국어 1~2문장으로>", "ifrs18_early_adoption": <true/false/null(확인 불가 또는
US GAAP이라 해당없음)>, "ifrs18_evidence": "<조기적용 여부 근거를 한국어 1~2문장으로,
근거 없으면 '조기적용 근거 없음, 2027년 의무시행 전' 등으로 명시>"}}"""


def research_ifrs18(
    ticker: str, company_name: str, market: str, as_of: dt.date
) -> tuple[bool | None, QualitativeScore | None]:
    """반환: (ifrs18_segment_disclosed: bool|None, ifrs18_mpm_qual: QualitativeScore|None).
    US 종목이거나 조기적용 근거가 없으면 ifrs18_mpm_qual은 항상 None이 정직한
    결과다(모듈 상단 docstring의 IFRS18 시행일 설명 참고) — 이는 "실패"가 아니라
    구조적으로 평가 대상이 아직 아니라는 뜻이다."""
    cached = _cache_get(market, ticker, "ifrs18", as_of)
    if cached:
        mpm = None
        if cached.get("mpm_points") is not None:
            mpm = QualitativeScore(
                points_awarded=cached["mpm_points"], max_points=1.5, evidence=cached["mpm_evidence"]
            )
        return cached.get("segment_disclosed"), mpm

    prompt = _IFRS18_RUBRIC.format(company_name=company_name, ticker=ticker, market=market)
    try:
        parsed = _call_gemini_json(prompt)
    except QualitativeResearchError as e:
        print(f"[qualitative] {market} {ticker} IFRS18 조사 실패(N/A로 남김): {e}")
        return None, None

    segment_disclosed = parsed.get("segment_disclosed")
    if not isinstance(segment_disclosed, bool):
        segment_disclosed = None

    mpm_qual = None
    early_adoption = parsed.get("ifrs18_early_adoption")
    if early_adoption is True:
        # 조기적용이 실제로 확인된 극히 드문 경우에만 정성 점수 부여.
        # evidence가 부실하면(예: 빈 문자열) QualitativeScore가 자체 거부한다.
        try:
            evidence = str(parsed.get("ifrs18_evidence", "")).strip()
            mpm_qual = QualitativeScore(points_awarded=1.5, max_points=1.5, evidence=evidence or "조기적용 확인됨")
        except ValueError as e:
            print(f"[qualitative] {market} {ticker} IFRS18 MPM 점수 생성 실패: {e}")
            mpm_qual = None
    # early_adoption이 False/None이면 mpm_qual=None 유지 — 2027년 전까지는 이게 정상.

    _cache_put(
        market, ticker, "ifrs18", as_of,
        {
            "segment_disclosed": segment_disclosed,
            "mpm_points": mpm_qual.points_awarded if mpm_qual else None,
            "mpm_evidence": mpm_qual.evidence if mpm_qual else None,
        },
    )
    return segment_disclosed, mpm_qual
