"""
main.py
일일 파이프라인 오케스트레이터 — KR 1종목 + US 1종목을 골라 데이터 수집 →
스코어카드 계산 → PDF 생성 → 텔레그램 전송까지 한 번에 실행한다.

=== 미확정 사항 (중요, 실사용 전 리오님 확인 필요) ===
종목 "선정" 로직(오늘 어떤 KR/US 종목을 다룰지)은 이 대화에서 아직 논의된 적이
없습니다. 아래 select_today_kr()/select_today_us()는 config/watchlist.yaml의
후보 리스트를 day-of-year 기준 단순 라운드로빈으로 도는 자리채움 구현입니다.
실제 운영 전에 반드시 확정 방식으로 교체해야 합니다(watchlist.yaml 상단 주석 참고).

=== 실행에 필요한 환경변수(전부 리오님이 직접 발급/설정) ===
- KRX_AUTH_KEY   (openapi.krx.co.kr)
- DART_API_KEY   (opendart.fss.or.kr)
- TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  (@BotFather)
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.report.pdf_generator import StockReportEntry, generate_pdf
from src.notify.telegram_notifier import send_pdf_report, TelegramNotifyError

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data" / "output"


def _load_watchlist() -> dict:
    with open(CONFIG_DIR / "watchlist.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def select_today_kr(watchlist: dict, as_of: dt.date) -> dict:
    """PLACEHOLDER: day-of-year 라운드로빈. 확정 알고리즘 아님(파일 상단 주석 참고)."""
    candidates = watchlist["kr_candidates"]
    idx = as_of.timetuple().tm_yday % len(candidates)
    return candidates[idx]


def select_today_us(watchlist: dict, as_of: dt.date) -> dict:
    """PLACEHOLDER: day-of-year 라운드로빈. 확정 알고리즘 아님(파일 상단 주석 참고)."""
    candidates = watchlist["us_candidates"]
    idx = as_of.timetuple().tm_yday % len(candidates)
    return candidates[idx]


def build_kr_report(ticker: str, market: str, as_of: dt.date):
    """KR 종목의 RawFinancials를 채워 ScoreReport를 계산한다.
    현재는 D블록(스트레스테스트)과 안전마진 게이트만 collector로 실측 채울 수 있고,
    ROE/이익잉여금 10년치/부채비율 등 나머지는 이 오케스트레이터에 아직 자동
    수집·매핑 로직이 연결되어 있지 않다(각 collector의 개별 함수는 이미 존재하지만,
    "어느 연도 범위를 어떻게 조합해 RawFinancials로 매핑할지"의 상세 로직은
    구현 범위 밖으로 남겨둠 — 다음 작업 단계로 안내 필요)."""
    raise NotImplementedError(
        "build_kr_report: collector 출력을 RawFinancials로 매핑하는 상세 로직이 "
        "아직 구현되지 않았습니다. src/collectors/{krx,dart}_collector.py의 개별 함수는 "
        "동작하지만, 이를 전부 호출해 12년 이익잉여금/5년 ROE/부채비율 등을 조합하는 "
        "'매핑 계층'이 다음에 필요합니다."
    )


def build_us_report(ticker: str, as_of: dt.date):
    """US 종목의 RawFinancials 매핑. KR과 동일한 이유로 아직 미구현."""
    raise NotImplementedError(
        "build_us_report: 아직 구현되지 않았습니다 — build_kr_report와 동일한 사유."
    )


def run_daily(as_of: dt.date | None = None, dry_run: bool = False) -> Path:
    as_of = as_of or dt.date.today()
    watchlist = _load_watchlist()
    kr_pick = select_today_kr(watchlist, as_of)
    us_pick = select_today_us(watchlist, as_of)

    print(f"[{as_of.isoformat()}] 오늘의 후보: KR={kr_pick['name']}({kr_pick['ticker']}), "
          f"US={us_pick['name']}({us_pick['ticker']})")

    kr_report = build_kr_report(kr_pick["ticker"], kr_pick["market"], as_of)
    us_report = build_us_report(us_pick["ticker"], as_of)

    entries = [
        StockReportEntry(report=kr_report),
        StockReportEntry(report=us_report),
    ]
    out_path = OUTPUT_DIR / f"report_{as_of.isoformat()}.pdf"
    generate_pdf(entries, as_of_date=as_of.isoformat(), output_path=out_path)
    print(f"PDF 생성 완료: {out_path}")

    if not dry_run:
        try:
            send_pdf_report(
                out_path,
                caption=f"Quality Stock Daily {as_of.isoformat()} — "
                        f"{kr_pick['name']} / {us_pick['name']}",
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
