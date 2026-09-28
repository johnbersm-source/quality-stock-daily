"""
scorecard.py
weights.yaml final_verdict_logic 전체를 실행하는 최상위 오케스트레이터.

1. 8블록 가산점수 산출 (blocks.py)
2. stress_test_gate, margin_of_safety_gate 판정 (gates.py)
3. final_grade 산출 — 게이트 중 하나라도 미통과/판정불가면 무조건 '관찰'
   (models.ScoreReport.final_grade에 구현됨)
4. 게이트 미통과 사유를 항상 함께 보고(gate_failure_reasons)
"""
from __future__ import annotations

from .blocks import ALL_BLOCK_SCORERS
from .gates import evaluate_all_gates
from .models import RawFinancials, ScoreReport


def compute_score(data: RawFinancials) -> ScoreReport:
    blocks = [scorer(data) for scorer in ALL_BLOCK_SCORERS]
    gates = evaluate_all_gates(data)
    return ScoreReport(
        ticker=data.ticker,
        company_name=data.company_name,
        market=data.market,
        as_of_date=data.as_of_date,
        blocks=blocks,
        gates=gates,
    )


def format_report_text(report: ScoreReport) -> str:
    """사람이 읽는 콘솔/로그용 요약(실제 PDF 리포트 생성은 src/report/ 모듈이 담당)."""
    lines = [
        f"=== {report.company_name}({report.ticker}, {report.market}) — {report.as_of_date} ===",
        f"총점: {report.total_score:.2f} / {report.total_possible:.0f}",
        f"최종 등급: {report.final_grade}",
        "",
    ]
    for b in report.blocks:
        flag = " [데이터 일부 결측]" if b.has_missing_data else ""
        lines.append(f"[{b.name}] {b.points_awarded:.2f} / {b.max_points:.0f}{flag}")
        for c in b.components:
            val = f"{c.points_awarded:.2f}" if c.points_awarded is not None else "N/A"
            lines.append(f"  - {c.name}: {val} / {c.max_points:.1f}  ({c.note})")
    lines.append("")
    lines.append("=== 게이트 ===")
    for g in report.gates:
        status = "통과" if g.passed is True else ("미통과" if g.passed is False else "판정불가")
        lines.append(f"[{g.gate_key}] {status} — {g.reason}")
    if report.final_grade == "관찰":
        lines.append("")
        lines.append("※ '관찰' 등급 사유: " + "; ".join(report.gate_failure_reasons()))
    return "\n".join(lines)
