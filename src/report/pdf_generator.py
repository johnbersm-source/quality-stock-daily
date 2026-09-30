"""
pdf_generator.py
ScoreReport(들)를 받아 xhtml2pdf + 번들 NanumGothic 폰트로 PDF 리포트를 생성한다.

WeasyPrint를 쓰지 않는 이유(사용자 표준 제약): Windows 개발 환경에서 WeasyPrint가
요구하는 GTK 계열 네이티브 의존성 설치가 번거롭고 문제를 일으켜, xhtml2pdf +
폰트 직접 번들 방식을 표준으로 채택했다(리포지토리 assets/fonts/에 실제
NanumGothic.ttf/NanumGothicBold.ttf 포함 — apt의 공식 fonts-nanum 패키지에서
가져온 정품 폰트 파일, 런타임에 원격 다운로드하지 않음. GitHub Actions
Ubuntu 러너에도 한글 폰트가 기본 설치되어 있지 않으므로 번들이 필수적이다).
"""
from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from xhtml2pdf import pisa

from ..scoring.models import ScoreReport
from .charts import generate_block_score_chart, generate_pdf_original_chart
from .plain_language import explain as plain_explain

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATE_DIR = Path(__file__).resolve().parent
FONT_REGULAR = PROJECT_ROOT / "assets" / "fonts" / "NanumGothic.ttf"
FONT_BOLD = PROJECT_ROOT / "assets" / "fonts" / "NanumGothicBold.ttf"

# 2026-09-30 신설 — 리오님 요청 사항 반영:
# 1) 게이트 이름 한글화("Shareholder Yield" 등 영어 용어도 포함)
# 2) 블록명의 "(신설, PDF 없음)" 같은 괄호를 없애고 대신 뱃지로 구분 표시
# 3) 점수 비율에 따른 긍정(빨강)/부정(파랑) 색상 클래스 판정
_GATE_KOREAN = {
    "stress_test_gate": "주가 방어력 테스트(과거 급락장 실측)",
    "margin_of_safety_gate": "안전마진 테스트(내재가치 대비 할인폭)",
}
_COMPONENT_KOREAN = {
    "Shareholder Yield": "주주환원율(배당+자사주)",
}
_BLOCK_ORIGIN = {
    "A_profitability_moat": "PDF원본 확장",
    "B_earnings_quality": "PDF원본 확장",
    "C_balance_sheet_resilience": "PDF원본 확장",
    "D_drawdown_defense": "v3 신설",
    "E_trend_growth": "v3 신설",
    "F_price": "PDF원본 확장",
    "G_shareholder_return_governance": "PDF원본 확장",
    "H_macro_catalyst": "v3 신설",
}


def _clean_block_name(name: str) -> str:
    """블록명 끝의 "(신설, PDF 없음)" 같은 괄호 설명을 제거한다 — 그 정보는
    이제 별도 뱃지(block_origin)로 표시하므로 이름 자체엔 필요 없다."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", name).strip()


def _gate_korean(gate_key: str) -> str:
    return _GATE_KOREAN.get(gate_key, gate_key)


def _component_korean(name: str) -> str:
    return _COMPONENT_KOREAN.get(name, name)


def _block_origin(block_key: str) -> str:
    return _BLOCK_ORIGIN.get(block_key, "")


_NUM_PATTERN = re.compile(r"[+-]?\d[\d,]*\.?\d*\s*%")


def _highlight_numbers(text: str) -> str:
    """설명(note) 텍스트 안의 핵심 수치(퍼센트 값)를 다른 색으로 강조한다
    (2026-09-30, 리오님 요청: "설명에 해당하는 내용 중 핵심 요소는 다른
    색으로 강조"). 빨강/파랑(긍정/부정 판정색)과 겹치지 않는 세 번째 색을
    쓴다 — CSS .highlight-num, 판정색과 혼동되지 않도록 골드/브라운 계열."""
    if not text:
        return text
    return _NUM_PATTERN.sub(lambda m: f'<span class="highlight-num">{m.group(0)}</span>', text)


def _perf_class(points_awarded: float | None, max_points: float) -> str:
    """긍정(빨강)/중립/부정(파랑) 색상 클래스 — 만점 대비 60% 이상=긍정,
    40% 미만=부정, 그 사이는 중립(리오님 요청: "점수가 기준치보다 높을때
    긍정적일때 진한 빨간색, 부정적일때 파란색")."""
    if points_awarded is None:
        return "perf-na"
    ratio = points_awarded / max_points if max_points else 0
    if ratio >= 0.6:
        return "perf-pos"
    if ratio < 0.4:
        return "perf-neg"
    return "perf-mid"


@dataclass
class StockReportEntry:
    report: ScoreReport
    narrative: str | None = None  # Gemini 등 내러티브 생성 단계에서 채워질 자유서술 코멘트(선택)
    chart_uri: str | None = field(default=None, repr=False)  # render_html이 내부적으로 채움(호출부에서 직접 설정 불필요)
    pdf_chart_uri: str | None = field(default=None, repr=False)  # PDF원본 10항목 득점률 차트(render_html이 채움)


class PdfGenerationError(Exception):
    pass


def _check_fonts():
    missing = [str(p) for p in (FONT_REGULAR, FONT_BOLD) if not p.exists()]
    if missing:
        raise PdfGenerationError(
            f"번들 NanumGothic 폰트 파일을 찾을 수 없습니다: {missing}. "
            f"assets/fonts/ 에 NanumGothic.ttf, NanumGothicBold.ttf가 포함되어 있는지 확인하세요."
        )


def _attach_charts(entries: list[StockReportEntry], chart_dir: Path) -> None:
    """각 종목의 8블록 점수 막대 그래프를 chart_dir에 PNG로 그려
    entry.chart_uri(file:// URI)를 채운다. 차트 생성이 실패해도(예: 폰트
    문제) 리포트 전체가 죽으면 안 되므로 실패 시 chart_uri=None으로 남기고
    템플릿에서 조용히 건너뛴다."""
    for i, entry in enumerate(entries):
        try:
            png_path = chart_dir / f"block_chart_{i}_{entry.report.ticker}.png"
            generate_block_score_chart(entry.report, png_path)
            entry.chart_uri = png_path.as_uri()
        except Exception as e:  # noqa: BLE001 - 차트는 부가 기능, 실패해도 리포트 생성은 계속
            print(f"[pdf_generator] {entry.report.ticker} 블록 점수 차트 생성 실패(차트 없이 진행): {e}")
            entry.chart_uri = None

        pdf_original_report = getattr(entry.report, "pdf_original", None)
        if pdf_original_report is not None:
            try:
                pdf_png_path = chart_dir / f"pdf_chart_{i}_{entry.report.ticker}.png"
                generate_pdf_original_chart(pdf_original_report, pdf_png_path)
                entry.pdf_chart_uri = pdf_png_path.as_uri()
            except Exception as e:  # noqa: BLE001 - 차트는 부가 기능, 실패해도 리포트 생성은 계속
                print(f"[pdf_generator] {entry.report.ticker} PDF원본 항목 차트 생성 실패(차트 없이 진행): {e}")
                entry.pdf_chart_uri = None


def render_html(entries: list[StockReportEntry], as_of_date: str, chart_dir: Path | None = None) -> str:
    """chart_dir을 주면 그 안에 종목별 블록 점수 PNG를 그려 각 entry.chart_uri를
    채운 뒤 렌더링한다(파일이 chart_dir에 실제로 존재해야 xhtml2pdf가 읽을 수
    있으므로, PDF 생성이 끝날 때까지 chart_dir이 삭제되지 않아야 한다 — 호출부
    generate_pdf() 참고). chart_dir을 생략하면(예: 차트 없이 HTML만 미리보기)
    차트 없이 렌더링한다."""
    _check_fonts()
    if chart_dir is not None:
        _attach_charts(entries, chart_dir)
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))
    env.globals["gate_korean"] = _gate_korean
    env.globals["component_korean"] = _component_korean
    env.globals["clean_block_name"] = _clean_block_name
    env.globals["block_origin"] = _block_origin
    env.globals["perf_class"] = _perf_class
    env.globals["plain_explain"] = plain_explain
    env.globals["highlight_numbers"] = _highlight_numbers
    template = env.get_template("template.html")
    return template.render(
        stocks=entries,
        as_of_date=as_of_date,
        # xhtml2pdf는 file:// URI로 로컬 폰트를 읽는다(상대경로 대신 절대경로 사용 —
        # GitHub Actions 등 실행 위치가 달라져도 항상 정확히 찾도록).
        font_regular_path=FONT_REGULAR.as_uri(),
        font_bold_path=FONT_BOLD.as_uri(),
    )


def generate_pdf(entries: list[StockReportEntry], as_of_date: str, output_path: str | Path) -> Path:
    """entries: [StockReportEntry(report=..., narrative=...), ...] — 보통 KR 1개 + US 1개.
    반환: 생성된 PDF 파일의 Path."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # xhtml2pdf(pisa)는 보안상 PROJECT_ROOT 바깥의 파일(예: 시스템 /tmp)을
    # file:// URI로 읽는 것을 거부한다("Blocked by the resource policy") —
    # 그래서 임시 차트 디렉터리도 반드시 PROJECT_ROOT 안에 만들어야 한다.
    tmp_root = PROJECT_ROOT / "data" / "tmp_charts"
    tmp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="qsd_charts_", dir=str(tmp_root)) as chart_dir_str:
        html = render_html(entries, as_of_date, chart_dir=Path(chart_dir_str))
        with open(output_path, "wb") as f:
            result = pisa.CreatePDF(src=html, dest=f, encoding="utf-8")

    if result.err:
        raise PdfGenerationError(f"PDF 생성 중 {result.err}건의 오류 발생(xhtml2pdf 로그 확인 필요)")

    return output_path
