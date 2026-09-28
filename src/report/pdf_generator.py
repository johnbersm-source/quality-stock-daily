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

from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from xhtml2pdf import pisa

from ..scoring.models import ScoreReport

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATE_DIR = Path(__file__).resolve().parent
FONT_REGULAR = PROJECT_ROOT / "assets" / "fonts" / "NanumGothic.ttf"
FONT_BOLD = PROJECT_ROOT / "assets" / "fonts" / "NanumGothicBold.ttf"


@dataclass
class StockReportEntry:
    report: ScoreReport
    narrative: str | None = None  # Gemini 등 내러티브 생성 단계에서 채워질 자유서술 코멘트(선택)


class PdfGenerationError(Exception):
    pass


def _check_fonts():
    missing = [str(p) for p in (FONT_REGULAR, FONT_BOLD) if not p.exists()]
    if missing:
        raise PdfGenerationError(
            f"번들 NanumGothic 폰트 파일을 찾을 수 없습니다: {missing}. "
            f"assets/fonts/ 에 NanumGothic.ttf, NanumGothicBold.ttf가 포함되어 있는지 확인하세요."
        )


def render_html(entries: list[StockReportEntry], as_of_date: str) -> str:
    _check_fonts()
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
    html = render_html(entries, as_of_date)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "wb") as f:
        result = pisa.CreatePDF(src=html, dest=f, encoding="utf-8")

    if result.err:
        raise PdfGenerationError(f"PDF 생성 중 {result.err}건의 오류 발생(xhtml2pdf 로그 확인 필요)")

    return output_path
