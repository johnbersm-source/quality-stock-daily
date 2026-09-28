"""
main.py
일일 파이프라인 오케스트레이터 — 후보군 전체 스코어링 → 게이트통과+최고점
선정(src/selection.py, 2026-09-28 리오님과 확정) → PDF 생성 → 텔레그램 전송.

=== 아직 미구현: collector -> RawFinancials 매핑 계층 ===
build_kr_report()/build_us_report()는 여전히 NotImplementedError를 던진다.
src/collectors/{krx,dart,us}_collector.py의 개별 함수(ROE, 이익잉여금
10년치, 부채비율, Owner Earnings 등)는 동작하지만, 이를 전부 호출해
src/scoring/models.py의 RawFinancials로 조합하는 코드가 다음 작업이다.
그 전까지는 select_daily_pick()에 넘길 score_fn이 실제 데이터를 낼 수
없어 이 파일을 그대로 실행하면 여기서 실패한다(선택 로직 자체는
tests/test_selection.py로 이미 별도 검증됨).

=== 실행에 필요한 환경변수 ===
- KRX_AUTH_KEY, DART_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (전부 사용자 발급)
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.notify.telegram_notifier import TelegramNotifyError, send_pdf_report
from src.report.pdf_generator import StockReportEntry, generate_pdf
from src.scoring.models import ScoreReport
from src.selection import SelectionResult, record_pick, select_daily_pick

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data" / "output"


def _load_watchlist() -> dict:
    with open(CONFIG_DIR / "watchlist.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_kr_report(candidate: dict, as_of: dt.date) -> ScoreReport:
    """KR 후보 1개를 collector 출력을 조합한 RawFinancials로 만들어 채점한다.
    (아직 매핑 계층 미구현 — README/main.py 상단 주석 참고)"""
    raise NotImplementedError(
        f"build_kr_report({candidate['ticker']}): collector 출력을 RawFinancials로 "
        f"매핑하는 로직이 아직 구현되지 않았습니다."
    )


def build_us_report(candidate: dict, as_of: dt.date) -> ScoreReport:
    """US 후보 1개 채점. KR과 동일한 사유로 아직 미구현."""
    raise NotImplementedError(
        f"build_us_report({candidate['ticker']}): 아직 구현되지 않았습니다."
    )


def _report_missing_pick(result: SelectionResult) -> str:
    lines = [f"[{result.market}] 오늘의 픽: 없음 — {result.reason}"]
    for i, r in enumerate(result.reference_top3, 1):
        gate_str = "게이트 " + ("통과" if r.all_gates_passed else "미통과")
        lines.append(f"  참고 {i}. {r.company_name}({r.ticker}) {r.total_score:.2f}점, {gate_str}")
    return "\n".join(lines)


def run_daily(as_of: dt.date | None = None, dry_run: bool = False) -> Path | None:
    as_of = as_of or dt.date.today()
    watchlist = _load_watchlist()
    sel_cfg = watchlist.get("selection", {})
    cooldown_days = sel_cfg.get("cooldown_days", 10)

    kr_result = select_daily_pick(
        "KR", watchlist["kr_candidates"],
        score_fn=lambda c: build_kr_report(c, as_of),
        as_of=as_of, cooldown_days=cooldown_days,
    )
    us_result = select_daily_pick(
        "US", watchlist["us_candidates"],
        score_fn=lambda c: build_us_report(c, as_of),
        as_of=as_of, cooldown_days=cooldown_days,
    )

    print(f"[{as_of.isoformat()}] {kr_result.reason}")
    print(f"[{as_of.isoformat()}] {us_result.reason}")

    if kr_result.picked is None:
        print(_report_missing_pick(kr_result))
    if us_result.picked is None:
        print(_report_missing_pick(us_result))

    entries = []
    if kr_result.picked is not None:
        entries.append(StockReportEntry(report=kr_result.picked))
    if us_result.picked is not None:
        entries.append(StockReportEntry(report=us_result.picked))

    if not entries:
        print("오늘은 KR/US 모두 게이트 통과 픽이 없어 PDF를 생성하지 않습니다.")
        return None

    out_path = OUTPUT_DIR / f"report_{as_of.isoformat()}.pdf"
    generate_pdf(entries, as_of_date=as_of.isoformat(), output_path=out_path)
    print(f"PDF 생성 완료: {out_path}")

    if kr_result.picked is not None:
        record_pick("KR", kr_result.picked.ticker, as_of)
    if us_result.picked is not None:
        record_pick("US", us_result.picked.ticker, as_of)

    if not dry_run:
        caption_parts = []
        if kr_result.picked:
            caption_parts.append(f"KR: {kr_result.picked.company_name}")
        if us_result.picked:
            caption_parts.append(f"US: {us_result.picked.company_name}")
        try:
            send_pdf_report(
                out_path,
                caption=f"Quality Stock Daily {as_of.isoformat()} — " + " / ".join(caption_parts),
            )
            print("텔레그램 전송 완료")
        except TelegramNotifyError as e:
            print(f"텔레그램 전송 실패(리포트 파일은 생성됨): {e}", file=sys.stderr)
            raise

    return out_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="quality-stock-daily 일일 파이프라인 실행")
    parser.add_argument("--dry-run", action="store_true", help="PDF만 생성하고 텔레그램 전송은 생략")
    parser.add_argument("--date", type=str, default=None, help="YYYY-MM-DD (기본값: 오늘)")
    args = parser.parse_args()

    target_date = dt.date.fromisoformat(args.date) if args.date else None
    run_daily(as_of=target_date, dry_run=args.dry_run)
