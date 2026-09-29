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

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from xhtml2pdf import pisa

from ..scoring.models import ScoreReport
from .charts import generate_block_score_chart

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATE_DIR = Path(__file__).resolve().parent
FONT_REGULAR = PROJECT_ROOT / "assets" / "fonts" / "NanumGothic.ttf"
FONT_BOLD = PROJECT_ROOT / "assets" / "fonts" / "NanumGothicBold.ttf"


@dataclass
class StockReportEntry:
    report: ScoreReport
    narrative: str | None = None  # Gemini 등 내러티브 생성 단계에서 채워질 자유서술 코멘트(선택)
    chart_uri: str | None = field(default=None, repr=False)  # render_html이 내부적으로 채움(호출부에서 직접 설정 불필요)


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
