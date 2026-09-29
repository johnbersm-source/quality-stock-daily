"""
selection.py
2026-09-28 리오님과 확정한 종목 선정 로직:

1. 후보군(src/universe.py가 매일 실행 시점 기준으로 동적 생성 — KR 코스피/코스닥
   시가총액 상위 N + US S&P500, config/pipeline.yaml에서 파라미터 설정) 전체를
   v3 스코어카드로 채점한다.
2. 최근 cooldown_days 일(달력일 기준, 단순화) 내 이미 픽으로 나온 종목은
   그날 후보에서 제외한다(data/pick_history.json에 기록).
3. 남은 후보 중 두 게이트(stress_test_gate, margin_of_safety_gate)를 모두
   통과한 종목만 픽 후보로 남긴다.
4. 픽 후보가 하나라도 있으면 총점 최고 종목을 오늘의 픽으로 선정한다.
5. 픽 후보가 하나도 없으면(전원 게이트 미통과, 또는 쿨다운으로 전부 제외)
   픽 없음으로 보고하고, 게이트 통과 여부와 무관하게 총점 상위 3개를
   "관찰 대상 참고용"으로만 병기한다 — 게이트를 임의로 완화해 억지로
   픽을 만들어내지 않는다(사용자 표준 원칙: 없는 사실을 지어내지 않음).
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .scoring.models import ScoreReport

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PICK_HISTORY_PATH = DATA_DIR / "pick_history.json"


@dataclass
class SelectionResult:
    market: str  # "KR" | "US"
    picked: ScoreReport | None
    reference_top3: list[ScoreReport] = field(default_factory=list)
    excluded_by_cooldown: list[str] = field(default_factory=list)
    all_candidates_scored: int = 0
    reason: str = ""


def _load_history() -> list[dict]:
    if not PICK_HISTORY_PATH.exists():
        return []
    return json.loads(PICK_HISTORY_PATH.read_text(encoding="utf-8"))


def _save_history(entries: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PICK_HISTORY_PATH.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


def recent_picks(market: str, as_of: dt.date, cooldown_days: int) -> set[str]:
    """최근 cooldown_days일 내 해당 시장에서 픽으로 기록된 티커 집합."""
    history = _load_history()
    cutoff = as_of - dt.timedelta(days=cooldown_days)
    return {
        h["ticker"] for h in history
        if h["market"] == market and cutoff < dt.date.fromisoformat(h["date"]) <= as_of
    }


def record_pick(market: str, ticker: str, as_of: dt.date) -> None:
    history = _load_history()
    history.append({"market": market, "ticker": ticker, "date": as_of.isoformat()})
    _save_history(history)


def select_daily_pick(
    market: str,
    candidates: list[dict],
    score_fn: Callable[[dict], ScoreReport],
    as_of: dt.date,
    cooldown_days: int = 10,
    qualitative_score_fn: Callable[[dict], ScoreReport] | None = None,
    qualitative_top_n: int = 20,
) -> SelectionResult:
    """candidates: src/universe.py의 get_kr_candidates()/get_us_candidates() 반환값.
    score_fn: 후보 dict(ticker/name/market 등)를 받아 ScoreReport를 반환하는 함수
    (실제로는 collector 호출 + scorecard.compute_score를 감싼 wrapper가 들어감).

    2026-09-29 2단계 채점 추가(리오님 지시 — 정성평가는 정량 상위 N종목에만
    적용): score_fn은 항상 정량 데이터만으로 850종목 전체를 1차 채점한다
    (기존과 동일, 정성평가 3항목은 N/A). qualitative_score_fn이 주어지면,
    1차 정량 총점 상위 qualitative_top_n개만 골라 Gemini 리서치로 정성평가
    (경제적 해자·IFRS18 MPM·거시)까지 채운 뒤 재채점하고, 그 결과로 최종
    랭킹·게이트 판정을 다시 계산한다. 나머지 (N-850)개는 정량 점수 그대로
    유지 — 어차피 순위권 밖이라 정성평가로도 뒤집힐 가능성이 낮고, 850종목
    전체에 매일 Gemini를 돌리는 건 API 호출량·시간상 비현실적이라는 판단."""
    excluded = recent_picks(market, as_of, cooldown_days)
    eligible = [c for c in candidates if c["ticker"] not in excluded]

    # 2026-09-29: 850종목 규모 1차 실행이 GitHub Actions 90분 타임아웃에
    # 걸려 강제취소됐는데, 진행상황 로그가 하나도 안 찍혀서(파이썬 stdout
    # 완전버퍼링 + 워크플로에 PYTHONUNBUFFERED 미설정) 병목 지점을 전혀
    # 알 수 없었다. 종목별 소요시간을 즉시 flush해서 어디가 느린지
    # 실측으로 확인할 수 있게 진행상황 로그를 추가한다.
    total = len(eligible)
    loop_start = time.monotonic()
    scored: list[ScoreReport] = []
    for i, c in enumerate(eligible, start=1):
        t0 = time.monotonic()
        try:
            scored.append(score_fn(c))
            status = "OK"
        except Exception as e:  # noqa: BLE001 - 개별 종목 실패로 전체 파이프라인이 죽으면 안 됨
            status = f"실패: {e}"
        elapsed = time.monotonic() - t0
        total_elapsed = time.monotonic() - loop_start
        avg = total_elapsed / i
        eta_remaining = avg * (total - i)
        print(
            f"[selection] {market} {i}/{total} {c['ticker']}({c.get('name')}) "
            f"{elapsed:.1f}s [{status}] | 누적 {total_elapsed:.0f}s, "
            f"평균 {avg:.1f}s/종목, 잔여 예상 {eta_remaining:.0f}s",
            flush=True,
        )

    scored.sort(key=lambda r: r.total_score, reverse=True)

    if qualitative_score_fn is not None and scored:
        candidates_by_ticker = {c["ticker"]: c for c in eligible}
        top_n = scored[:qualitative_top_n]
        print(
            f"[selection] {market} 정성평가 2단계: 정량 상위 {len(top_n)}개 재채점 시작"
            f"(Gemini 리서치, 종목당 수십 초 소요될 수 있음)",
            flush=True,
        )
        requalified: list[ScoreReport] = []
        for i, r in enumerate(top_n, start=1):
            c = candidates_by_ticker.get(r.ticker)
            if c is None:
                requalified.append(r)
                continue
            t0 = time.monotonic()
            try:
                new_report = qualitative_score_fn(c)
                requalified.append(new_report)
                status = "OK"
            except Exception as e:  # noqa: BLE001 - 정성평가 실패로 정량 결과까지 잃으면 안 됨
                requalified.append(r)  # 실패 시 정량 결과라도 그대로 유지
                status = f"정성평가 실패(정량 결과 유지): {e}"
            print(
                f"[selection] {market} 정성평가 {i}/{len(top_n)} {r.ticker}({r.company_name}) "
                f"{time.monotonic() - t0:.1f}s [{status}]", flush=True,
            )
        scored = requalified + scored[qualitative_top_n:]
        scored.sort(key=lambda r: r.total_score, reverse=True)

    gate_passed = [r for r in scored if r.all_gates_passed]

    if gate_passed:
        picked = gate_passed[0]
        reason = (f"{len(eligible)}개 후보 중 {len(gate_passed)}개 게이트 통과, "
                  f"최고점 {picked.company_name}({picked.total_score:.2f}점) 선정")
        return SelectionResult(
            market=market, picked=picked, reference_top3=scored[:3],
            excluded_by_cooldown=sorted(excluded), all_candidates_scored=len(scored),
            reason=reason,
        )

    reason = (f"{len(eligible)}개 후보 전부 게이트 미통과(또는 채점 실패) — 픽 없음. "
              f"점수 상위 3개는 참고용으로만 병기")
    return SelectionResult(
        market=market, picked=None, reference_top3=scored[:3],
        excluded_by_cooldown=sorted(excluded), all_candidates_scored=len(scored),
        reason=reason,
    )
