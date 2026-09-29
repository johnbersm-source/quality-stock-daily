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

# 컴포넌트별 캐시 유효기간(일) — 해자/IFRS18/상법개정 수혜는 자주 안 바뀌니 길게,
# 거시/사이클은 시황·촉매가 매일 바뀌므로 짧게.
_TTL_DAYS = {"moat": 90, "ifrs18": 90, "macro": 1, "commercial_code": 90}

_MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest")

# 2026-09-29 실측(diagnose_gemini_quota): 결제 계정 미연결 무료 키에서는
# Google Search grounding이 켜진 호출이 모델을 가리지 않고 전부 429
# RESOURCE_EXHAUSTED로 막혔다(gemini-3.8-flash 포함). 리오님이 결제 카드
# 연결을 원치 않는다는 명시적 지시(2026-09-29)에 따라, grounding 없이도
# 성공이 실측 확인된 gemini-flash-lite-latest를 기본값으로 전환한다.
# 이 결정의 트레이드오프(정직하게 인지해야 함): 실시간 웹검색 근거 없이
# 모델의 학습 지식만으로 채점하므로, evidence는 "지금 이 순간의 사실"이
# 아니라 "모델 학습 시점까지 알려진 사실"일 수 있다 — 그래서 아래 각
# 루브릭 프롬프트에 이 사실을 명시해 결과에도 투명하게 남기고, 매크로처럼
# 최신성이 특히 중요한 항목은 이 한계가 더 크다는 점을 evidence에 모델이
# 스스로 명시하도록 요구한다.
_USE_GROUNDING_DEFAULT = os.environ.get("GEMINI_USE_GROUNDING", "0") == "1"


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


def _call_gemini_json(prompt: str, model: str | None = None, use_grounding: bool | None = None) -> dict:
    """Gemini를 호출해 JSON 응답을 파싱한다. 실패 시 QualitativeResearchError를
    던진다(호출부에서 잡아 None 처리).

    2026-09-29: 무료 API 키(결제 계정 미연결)로 Google Search grounding을
    켜면 429 RESOURCE_EXHAUSTED가 발생하는 게 실측 확인됐다(리오님이 결제
    카드 연결을 원치 않아 이 경로를 그대로 씀). use_grounding=False로 순수
    텍스트 생성만 시도하면 무료 티어에서도 되는지 diagnose_gemini_quota()로
    조합별로 실측 확인한다 — 이 함수 자체는 두 모드 다 지원하도록 파라미터화."""
    from google.genai import types

    if use_grounding is None:
        use_grounding = _USE_GROUNDING_DEFAULT

    client = _client()
    tools = [types.Tool(google_search=types.GoogleSearch())] if use_grounding else None
    config = types.GenerateContentConfig(
        tools=tools,
        temperature=0.1,  # 채점 일관성 우선 — 창의성 불필요
    )
    try:
        response = client.models.generate_content(
            model=model or _MODEL_NAME, contents=prompt, config=config,
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


def diagnose_gemini_quota() -> list[dict]:
    """2026-09-29 신설: 리오님이 결제 카드 연결을 원치 않아, 어떤 (모델, grounding
    여부) 조합이 결제 계정 미연결 무료 키로도 실제 동작하는지 최소 비용(짧은
    프롬프트, JSON 아님)으로 경험적으로 확인한다. Google Search grounding이
    결제 계정 없이는 아예 막혀 있고 순수 텍스트 생성은 된다는 보고가 있어
    (README 참고), 그 가설을 실측으로 검증하기 위한 진단 전용 함수."""
    from google.genai import types

    combos = [
        ("gemini-3.8-flash", True),
        ("gemini-3.8-flash", False),
        ("gemini-2.5-flash", True),
        ("gemini-2.5-flash", False),
        ("gemini-2.0-flash", True),
        ("gemini-2.0-flash", False),
        ("gemini-flash-lite-latest", False),
    ]
    client = _client()
    results = []
    for model, grounding in combos:
        tools = [types.Tool(google_search=types.GoogleSearch())] if grounding else None
        config = types.GenerateContentConfig(tools=tools, temperature=0.1)
        try:
            response = client.models.generate_content(
                model=model, contents="한 단어로만 답하세요: OK", config=config,
            )
            results.append({"model": model, "grounding": grounding, "ok": True, "text": (response.text or "")[:50]})
        except Exception as e:  # noqa: BLE001
            results.append({"model": model, "grounding": grounding, "ok": False, "error": str(e)[:200]})
    return results


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

_NO_GROUNDING_NOTICE = """**중요(모드 안내)**: 이 호출에는 실시간 Google 검색 도구가 연결되어
있지 않습니다. 당신의 학습(훈련) 데이터에 들어있는 지식만으로 판단하세요.
학습 시점 이후에 일어났을 수 있는 변화(최신 실적, 최근 뉴스, 규제 변경 등)는
알 수 없다는 점을 스스로 인지하고, 확신이 없는 부분은 점수를 보수적으로
낮추세요. evidence 문장 끝에 반드시 "(실시간 검색 아님, 학습 지식 기반)"이라는
표현을 포함해 이 한계를 명시하세요."""


_MOAT_RUBRIC = """당신은 가치투자 애널리스트입니다. 다음 기업의 경제적 해자(economic moat)를
0~{max_points}점으로 채점하세요.

{grounding_notice}

기업: {company_name} ({ticker}, {market} 시장)

채점 기준(반드시 이 기준을 따르세요):
- {max_points}점: 구조적 해자(네트워크효과·높은 전환비용·무형자산/브랜드·비용우위·
  규제 진입장벽) 중 2개 이상이 뚜렷하고, 장기 ROIC가 WACC를 꾸준히 상회하거나
  경쟁사 대비 마진이 확연히 높은 등 실제 데이터로 뒷받침됨.
- {mid_high}점 내외: 해자 1개가 뚜렷하나 부분적으로만 데이터로 뒷받침됨, 또는
  경쟁 압력이 일부 존재.
- {mid_low}점 내외: 약하거나 일시적인 해자, 경쟁 심화 신호가 뚜렷함.
- 0~1점: 커머디티 성격이 강하고 뚜렷한 해자 근거를 찾지 못함.

가능한 한 구체적 사실(시장점유율, 마진 비교, 가격결정력 사례, 실적/공시 등)에
근거해서 판단하고, 근거 없는 추측은 하지 마세요. 확실한 근거가 부족하다면
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
        grounding_notice=_NO_GROUNDING_NOTICE if not _USE_GROUNDING_DEFAULT else "",
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
전환 등)를 0~{max_points}점으로 채점하세요.

{grounding_notice}
**추가 유의**: 이 항목(거시·사이클·재료)은 특히 최신성이 중요합니다. 실시간
검색 없이 학습 지식만으로 답하는 경우, 당신이 아는 가장 최근 상황이 실제
{as_of} 시점과 다를 수 있다는 점을 evidence에 반드시 함께 명시하고, 확신이
낮으면 중간 점수({mid_high}점 내외) 이하로 보수적으로 채점하세요.

기업: {company_name} ({ticker}, {market} 시장)

채점 기준:
- {max_points}점: 섹터가 뚜렷한 매크로 순풍(금리/업황 사이클상 유리)을 받고
  있고, 향후 1~2분기 내 긍정적 촉매(실적 모멘텀, 우호적 정책/규제, 업황 전환)가
  임박함.
- {mid_high}점 내외: 중립~약한 순풍, 촉매는 불확실하거나 아직 먼 미래.
- {mid_low}점 내외: 중립~약한 역풍.
- 0~1점: 뚜렷한 역풍(업황 다운사이클, 규제 리스크, 수요 둔화) 또는 임박한
  부정적 촉매.

가능한 한 구체적 사실·데이터에 근거해서 판단하고, 근거 없는 추측은 하지 마세요.

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
        grounding_notice=_NO_GROUNDING_NOTICE if not _USE_GROUNDING_DEFAULT else "",
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
공식 발표한 근거가 있는지 답하세요.

{grounding_notice}
확실한 근거가 없으면(특히 실시간 검색 없이 학습 지식만으로 확인이 안 되면)
반드시 false/null로 답하세요 — 불확실할 때 true로 답하는 것보다 모른다고
답하는 편이 훨씬 안전합니다.

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

    prompt = _IFRS18_RUBRIC.format(
        company_name=company_name, ticker=ticker, market=market,
        grounding_notice=_NO_GROUNDING_NOTICE if not _USE_GROUNDING_DEFAULT else "",
    )
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


# --- G블록: 상법개정 수혜(업종) (3점, PDF 원본 항목 ⑩ 대응, 2026-09-30 신설) ---
#
# 2026-09-30: PDF 원본 vs v3 비교 부록을 만들면서 발견한 갭 — 원본 항목 ⑩
# "상법개정 수혜"(직접 수혜 업종(지주·금융·자산주) + 선제 대응→10 / 간접
# 수혜→5 / 해당없음→2)가 v3에는 소각비율 컴포넌트 이름에만 흔적으로 남아있고
# 실제로 채점되지 않고 있었다. 리오님이 별도 컴포넌트 신설을 지시해 추가.
#
# 상법(한국 상법) 개정은 한국 기업에만 적용되는 제도이므로, US 종목은 IFRS18과
# 마찬가지로 구조적으로 해당 없음(None)이 정직한 결과다 — 0점이 아니라 "평가
# 대상 자체가 아님".

_COMMERCIAL_CODE_RUBRIC = """당신은 한국 지배구조 전문 애널리스트입니다. 다음 기업이
한국 상법 개정(이사의 충실의무를 회사뿐 아니라 주주 전체로 확대, 집중투표제
활성화, 감사위원 분리선출 확대 등 지배구조·주주권 강화 방향)의 수혜 업종에
해당하는지, 그리고 그에 선제적으로 대응(지배구조 개편, 주주환원 확대, 밸류업
계획 공시 등)했는지 0~{max_points}점으로 채점하세요.

{grounding_notice}

기업: {company_name} ({ticker}, {market} 시장)

**중요**: {market} 시장 기업이 미국 등 한국 상법 적용을 받지 않는 곳이라면
애초에 이 항목 자체가 적용 대상이 아니므로 applicable을 반드시 false로
답하세요.

채점 기준 (applicable=true인 경우에만):
- {max_points}점: 지주회사·금융회사(은행/보험/증권)·순자산가치 대비 저평가된
  자산주 등 직접 수혜 업종에 해당하고, 지배구조 개편이나 주주환원 확대 등
  선제 대응 근거도 있음.
- {mid}점 내외: 직접 수혜 업종은 아니지만 지배구조 개선의 간접 수혜가
  예상되거나(예: 대기업집단 계열사), 선제 대응 근거가 불확실함.
- 0~1점: 해당 업종도 아니고 선제 대응 근거도 없음.

확실한 근거가 없으면 낮은 점수를 주고 그 사실을 evidence에 명시하세요.

아래 JSON 형식으로만 답하세요(다른 텍스트 없이):
{{"applicable": <true/false>, "points": <applicable=true일 때만 0~{max_points}
사이 숫자, false면 null>, "evidence": "<채점 근거를 한국어 2~4문장으로,
업종 분류와 선제 대응 여부 포함. applicable=false면 그 이유를 명시>"}}"""


def research_commercial_code_benefit(
    ticker: str, company_name: str, market: str, as_of: dt.date
) -> QualitativeScore | None:
    """반환: QualitativeScore|None. market="US"이거나 Gemini가 applicable=false로
    답하면 None이 정직한 결과다(한국 상법은 한국 기업에만 적용 — IFRS18의 US
    GAAP 비적용과 동일한 구조)."""
    if market == "US":
        return None  # 한국 상법은 미국 상장사에 애초에 적용되지 않음 — 조사 자체가 무의미

    cached = _cache_get(market, ticker, "commercial_code", as_of)
    if cached:
        if cached.get("points") is None:
            return None
        return QualitativeScore(points_awarded=cached["points"], max_points=3, evidence=cached["evidence"])

    prompt = _COMMERCIAL_CODE_RUBRIC.format(
        max_points=3, mid=1.5, company_name=company_name, ticker=ticker, market=market,
        grounding_notice=_NO_GROUNDING_NOTICE if not _USE_GROUNDING_DEFAULT else "",
    )
    try:
        parsed = _call_gemini_json(prompt)
    except QualitativeResearchError as e:
        print(f"[qualitative] {market} {ticker} 상법개정 수혜 조사 실패(N/A로 남김): {e}")
        return None

    applicable = parsed.get("applicable")
    score = None
    if applicable is True and parsed.get("points") is not None:
        try:
            score = _build_qual_score(parsed, max_points=3, component_label="상법개정 수혜(업종)")
        except QualitativeResearchError as e:
            print(f"[qualitative] {market} {ticker} 상법개정 수혜 점수 생성 실패(N/A로 남김): {e}")
            score = None
    # applicable=False 또는 points 없음 → score=None 유지(구조적 미적용/판단불가, 0점 아님)

    _cache_put(
        market, ticker, "commercial_code", as_of,
        {"points": score.points_awarded if score else None, "evidence": score.evidence if score else None},
    )
    return score
