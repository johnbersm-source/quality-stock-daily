"""
test_selection.py
selection.py의 선정 로직(게이트통과+최고점, 쿨다운, 전원미통과 폴백)을
합성(synthetic) ScoreReport로 검증한다.

주의: 이건 실제 시세/재무 데이터를 쓰는 테스트가 아니라 "선정 알고리즘
자체"의 단위테스트다. 실제 종목 데이터로 하는 종단 테스트는 main.py의
build_kr_report/build_us_report(아직 미구현) 완성 후에나 가능하다 — 그
전까지 이 테스트가 선정 로직의 정확성을 담보하는 역할을 한다.
"""
import datetime as dt
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.selection as selection  # noqa: E402
from src.scoring.models import BlockResult, ComponentResult, GateResult, ScoreReport  # noqa: E402


def make_report(ticker: str, name: str, score: float, gates_pass: bool) -> ScoreReport:
    block = BlockResult("X", "테스트블록", 100, [ComponentResult("c", score, 100, "synthetic")])
    gates = [
        GateResult("stress_test_gate", gates_pass, {}, "synthetic"),
        GateResult("margin_of_safety_gate", gates_pass, {}, "synthetic"),
    ]
    return ScoreReport(ticker=ticker, company_name=name, market="KR",
                        as_of_date="2026-09-28", blocks=[block], gates=gates)


def run_test():
    # 임시 디렉터리로 pick_history 경로를 바꿔치기해 테스트 격리
    tmpdir = tempfile.mkdtemp()
    selection.DATA_DIR = Path(tmpdir)
    selection.PICK_HISTORY_PATH = Path(tmpdir) / "pick_history.json"

    candidates = [
        {"ticker": "A", "name": "종목A(고득점,게이트통과)"},
        {"ticker": "B", "name": "종목B(더고득점,게이트미통과)"},
        {"ticker": "C", "name": "종목C(중간득점,게이트통과)"},
    ]

    def score_fn(c):
        table = {
            "A": make_report("A", c["name"], 75, True),
            "B": make_report("B", c["name"], 95, False),  # 점수는 제일 높지만 게이트 미통과
            "C": make_report("C", c["name"], 60, True),
        }
        return table[c["ticker"]]

    as_of = dt.date(2026, 9, 28)

    # --- 케이스 1: 게이트 통과 종목 중 최고점(A, 75점)이 선정되어야 함 (B는 미통과라 제외) ---
    result = selection.select_daily_pick("KR", candidates, score_fn, as_of, cooldown_days=10)
    assert result.picked is not None, "게이트 통과 종목(A, C)이 있으므로 픽이 나와야 함"
    assert result.picked.ticker == "A", f"최고 게이트통과 종목(A)이 선정되어야 하는데 {result.picked.ticker}가 선정됨"
    print(f"[케이스1 PASS] 선정: {result.picked.ticker} ({result.picked.total_score}점) — {result.reason}")

    selection.record_pick("KR", "A", as_of)

    # --- 케이스 2: A가 쿨다운으로 제외 -> C(60점, 게이트통과)가 선정되어야 함 ---
    result2 = selection.select_daily_pick("KR", candidates, score_fn, as_of, cooldown_days=10)
    assert "A" in result2.excluded_by_cooldown, "직전에 픽으로 기록된 A는 쿨다운으로 제외되어야 함"
    assert result2.picked is not None and result2.picked.ticker == "C", \
        f"A 제외 후 남은 게이트통과 종목(C)이 선정되어야 하는데 {result2.picked}"
    print(f"[케이스2 PASS] A 쿨다운 제외 확인, 선정: {result2.picked.ticker} — {result2.reason}")

    # --- 케이스 3: 전원 게이트 미통과 -> 픽 없음 + 상위3 참고용 ---
    # (케이스1/2의 쿨다운 기록은 이 케이스와 무관한 별도 기능이므로 초기화)
    selection._save_history([])

    def score_fn_all_fail(c):
        return make_report(c["ticker"], c["name"], {"A": 75, "B": 95, "C": 60}[c["ticker"]], False)

    result3 = selection.select_daily_pick("KR", candidates, score_fn_all_fail, as_of, cooldown_days=10)
    assert result3.picked is None, "전원 게이트 미통과면 픽이 없어야 함(억지로 만들면 안 됨)"
    assert len(result3.reference_top3) == 3, "참고용 상위 3개는 병기되어야 함"
    assert result3.reference_top3[0].ticker == "B", "참고용 1위는 점수 기준 최고(B, 95점)여야 함"
    print(f"[케이스3 PASS] 픽 없음 확인, 참고용 상위: "
          f"{[r.ticker for r in result3.reference_top3]} — {result3.reason}")

    print("\nALL PASS")


if __name__ == "__main__":
    run_test()
