"""
charts.py
리포트에 그래프/차트가 전혀 없다는 리오님 지적(2026-09-29)에 대한 보강.

xhtml2pdf는 JS/canvas 기반 인터랙티브 차트를 그릴 수 없고 SVG 지원도
불안정해서(폰트 임베딩과 함께 쓰면 특히), matplotlib으로 정적 PNG를
서버에서 미리 그려 파일 경로(file:// URI)로 <img> 임베드하는 방식을
쓴다 — 폰트를 assets/fonts/NanumGothic.ttf에서 직접 번들해 쓰는 기존
PDF 생성 방식(pdf_generator.py 상단 주석 참고)과 동일한 원칙.

1차 범위(2026-09-30, 리오님 확인 후 확장 가능):
- 8블록 점수 막대 그래프: 종목별로 A~H 블록 각각이 만점 대비 몇 %를
  받았는지 한눈에 보여준다 — 표만으로는 안 보이던 "어느 블록이 약한가"
  라는 형태(shape)를 시각적으로 드러내는 게 목적.
데이터 결측(N/A)인 컴포넌트만 있는 블록은 0%가 아니라 회색으로 표시해
"저평가"와 "데이터 없음"을 혼동하지 않도록 한다(리포트 전체의 N/A 원칙과
동일 — models.py, disclaimer 참고).
"""
from __future__ import annotations

import re

import matplotlib

matplotlib.use("Agg")  # 서버(헤드리스) 환경 — GUI 백엔드 불필요/불가

from pathlib import Path

import matplotlib.font_manager as fm
import matplotlib.pyplot as plt

from ..scoring.models import ScoreReport
from ..scoring.pdf_original import PdfOriginalReport

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
FONT_REGULAR = PROJECT_ROOT / "assets" / "fonts" / "NanumGothic.ttf"

_MISSING_COLOR = "#c9c9c9"  # 데이터 결측 블록(회색 — "0점"과 시각적으로 구분)
_FILLED_COLOR = "#2f5fa8"


def _clean_block_name(name: str) -> str:
    """블록명 끝의 "(신설, PDF 없음)" 같은 괄호 설명을 제거한다 — pdf_generator.py의
    동일 이름 함수와 로직을 맞춤(2026-09-30, 리오님 요청: 괄호 표기 제거 —
    표 뿐 아니라 차트 라벨도 동일하게 정리해야 한다)."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", name).strip()


def _korean_font_prop() -> fm.FontProperties | None:
    if not FONT_REGULAR.exists():
        return None
    fm.fontManager.addfont(str(FONT_REGULAR))
    return fm.FontProperties(fname=str(FONT_REGULAR))


def generate_block_score_chart(report: ScoreReport, output_path: Path) -> Path:
    """종목 1개의 8블록 점수를 만점 대비 %로 보여주는 가로 막대 그래프를
    PNG로 그려 output_path에 저장하고 그 Path를 반환한다.

    블록의 모든 컴포넌트가 N/A(데이터 결측)면 그 블록은 0%가 아니라
    회색 막대로 표시해 "저평가"와 "데이터 없음"을 혼동하지 않게 한다."""
    font_prop = _korean_font_prop()

    names: list[str] = []
    pct: list[float] = []
    colors: list[str] = []
    labels: list[str] = []

    # PDF 렌더링(위에서 아래로 읽는 순서) 그대로, 막대 그래프는 위→아래가
    # 표와 같은 순서가 되도록 나중에 reverse해서 그린다(맨 위 A블록).
    for block in report.blocks:
        has_any_data = any(c.points_awarded is not None for c in block.components)
        names.append(_clean_block_name(block.name))
        if has_any_data:
            ratio = block.points_awarded / block.max_points if block.max_points else 0.0
            pct.append(round(ratio * 100, 1))
            colors.append(_FILLED_COLOR)
            labels.append(f"{block.points_awarded:.1f}/{block.max_points:.0f}")
        else:
            pct.append(0.0)
            colors.append(_MISSING_COLOR)
            labels.append("데이터 결측")

    names = names[::-1]
    pct = pct[::-1]
    colors = colors[::-1]
    labels = labels[::-1]

    fig, ax = plt.subplots(figsize=(6.0, 2.6), dpi=150)
    y_pos = range(len(names))
    bars = ax.barh(y_pos, pct, color=colors, height=0.62)

    kwargs = {"fontproperties": font_prop} if font_prop else {}
    ax.set_yticks(list(y_pos))
    ax.set_yticklabels(names, fontsize=8, **kwargs)
    ax.set_xlim(0, 100)
    ax.set_xlabel("만점 대비 득점률 (%)", fontsize=8, **kwargs)
    ax.tick_params(axis="x", labelsize=7)

    for bar, label in zip(bars, labels):
        width = bar.get_width()
        x = width + 2 if width < 92 else width - 2
        ha = "left" if width < 92 else "right"
        text_color = "#333333" if width < 92 else "white"
        ax.text(
            x, bar.get_y() + bar.get_height() / 2, label,
            va="center", ha=ha, fontsize=7, color=text_color, **kwargs,
        )

    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight")
    plt.close(fig)
    return output_path


def generate_pdf_original_chart(report: PdfOriginalReport, output_path: Path) -> Path:
    """PDF 원본 10항목 각각의 만점(10점) 대비 득점률을 v3 블록 차트와 동일한
    스타일로 그린다(2026-09-30, 리오님 요청: "블록별 득점률도 각각 그래프를
    제시" — v3용은 위의 generate_block_score_chart, 이건 PDF원본용). N/A
    항목은 동일하게 회색으로 구분한다."""
    font_prop = _korean_font_prop()

    names: list[str] = []
    pct: list[float] = []
    colors: list[str] = []
    labels: list[str] = []

    for item in report.items:
        names.append(item.name)
        if item.points_awarded is not None:
            ratio = item.points_awarded / item.max_points if item.max_points else 0.0
            pct.append(round(ratio * 100, 1))
            colors.append(_FILLED_COLOR)
            labels.append(f"{item.points_awarded:.0f}/{item.max_points:.0f}")
        else:
            pct.append(0.0)
            colors.append(_MISSING_COLOR)
            labels.append("데이터 결측")

    names = names[::-1]
    pct = pct[::-1]
    colors = colors[::-1]
    labels = labels[::-1]

    fig, ax = plt.subplots(figsize=(6.0, 3.0), dpi=150)
    y_pos = range(len(names))
    bars = ax.barh(y_pos, pct, color=colors, height=0.62)

    kwargs = {"fontproperties": font_prop} if font_prop else {}
    ax.set_yticks(list(y_pos))
    ax.set_yticklabels(names, fontsize=8, **kwargs)
    ax.set_xlim(0, 100)
    ax.set_xlabel("만점(10점) 대비 득점률 (%) — PDF 원본 기준", fontsize=8, **kwargs)
    ax.tick_params(axis="x", labelsize=7)

    for bar, label in zip(bars, labels):
        width = bar.get_width()
        x = width + 2 if width < 92 else width - 2
        ha = "left" if width < 92 else "right"
        text_color = "#333333" if width < 92 else "white"
        ax.text(
            x, bar.get_y() + bar.get_height() / 2, label,
            va="center", ha=ha, fontsize=7, color=text_color, **kwargs,
        )

    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight")
    plt.close(fig)
    return output_path
