"""
test_pdf_generation.py
scorecard.compute_score() 결과(실측 삼성전자 데이터, test_scorecard_samsung.py 재사용)로
실제 PDF를 생성해 xhtml2pdf + 번들 NanumGothic 경로가 실제로 동작하는지 검증한다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.report.pdf_generator import StockReportEntry, generate_pdf
from src.scoring.scorecard import compute_score
from test_scorecard_samsung import data as samsung_data  # noqa: E402

if __name__ == "__main__":
    report = compute_score(samsung_data)
    entry = StockReportEntry(
        report=report,
        narrative=(
            "삼성전자는 메모리 반도체 부문에서 견조한 시장 지위를 유지하고 있으나, "
            "2026년 6~7월 반도체발 급락 국면에서 코스피 대비 더 크게 하락(스트레스테스트 게이트 미통과)했고, "
            "현재 시가총액이 DCF 기준 내재가치를 크게 상회해 안전마진 게이트도 통과하지 못했습니다. "
            "펀더멘털 지표(Altman Z-Score 9.40 등) 자체는 우수하지만, 두 게이트 중 하나라도 실패하면 "
            "v3 방법론상 총점과 무관하게 '관찰' 등급으로 분류됩니다."
        ),
    )
    out_path = Path(__file__).resolve().parent.parent / "data" / "output" / "test_samsung_report.pdf"
    result_path = generate_pdf([entry], as_of_date="2026-09-28", output_path=out_path)
    size = result_path.stat().st_size
    print(f"PDF 생성 완료: {result_path} ({size:,} bytes)")
    assert size > 5000, "PDF 파일 크기가 비정상적으로 작습니다(생성 실패 가능성)"
    print("PASS")
