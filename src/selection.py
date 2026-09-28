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
) -> SelectionResult:
    """candidates: src/universe.py의 get_kr_candidates()/get_us_candidates() 반환값.
    score_fn: 후보 dict(ticker/name/market 등)를 받아 ScoreReport를 반환하는 함수
    (실제로는 collector 호출 + scorecard.compute_score를 감싼 wrapper가 들어감)."""
    excluded = recent_picks(market, as_of, cooldown_days)
    eligible = [c for c in candidates if c["ticker"] not in excluded]

    scored: list[ScoreReport] = []
    for c in eligible:
        try:
            scored.append(score_fn(c))
        except Exception as e:  # noqa: BLE001 - 개별 종목 실패로 전체 파이프라인이 죽으면 안 됨
            print(f"[selection] {market} {c['ticker']}({c.get('name')}) 채점 실패, 스킵: {e}")

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
