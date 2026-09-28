"""
telegram_notifier.py
완성된 일일 PDF 리포트를 텔레그램 봇으로 전송하는 모듈.

=== 사용 전 필수 준비 ===
1. 텔레그램 @BotFather 에게 /newbot 으로 봇 생성 → TELEGRAM_BOT_TOKEN 발급(무료)
2. 리포트를 받을 채팅(개인 DM 또는 채널/그룹)의 chat_id 확인 → TELEGRAM_CHAT_ID
   (가장 쉬운 방법: 봇을 채팅에 추가한 뒤 https://api.telegram.org/bot<TOKEN>/getUpdates
   호출해서 결과에 찍히는 chat.id 값을 사용)
3. 환경변수 TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 설정

주의: 리오님 메모리에 "회사/학교 관리 네트워크에서 텔레그램 앱 자체가 차단됨"이라는
기록이 있으나, 이 파이프라인은 GitHub Actions 클라우드 러너에서 실행되므로
사용자 로컬 네트워크 제한과 무관하게 api.telegram.org에 접근한다 — 단, 리오님이
"받는 쪽"에서 텔레그램 메시지를 확인하려면 그 PC가 아닌 다른 기기/네트워크
(예: 아이폰, 리오님이 주로 쓰시는 기기)를 사용해야 함을 참고.

=== 검증 상태 (2026-09-28) ===
- api.telegram.org 리처빌리티: curl로 확인 완료(HTTP 404 + 구조화된 JSON 에러,
  잘못된 토큰 형식에 대한 텔레그램 표준 응답 — 서버 자체는 정상 동작).
- 실제 봇 토큰으로 라이브 전송 테스트는 미수행(이 세션은 TELEGRAM_BOT_TOKEN을
  보유하지 않음) — __main__ 진단 모드로 토큰 발급 후 1회 실행하면 바로 검증 가능.
"""
from __future__ import annotations

import os
from pathlib import Path

import requests

BASE_URL = "https://api.telegram.org/bot{token}/{method}"

# 텔레그램 sendDocument는 캡션 길이를 1024자로 제한한다(공식 API 문서 기준).
TELEGRAM_CAPTION_LIMIT = 1024


class TelegramNotifyError(Exception):
    pass


def _credentials() -> tuple[str, str]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise TelegramNotifyError(
            "TELEGRAM_BOT_TOKEN 및/또는 TELEGRAM_CHAT_ID 환경변수가 설정되지 않았습니다. "
            "@BotFather로 봇 생성 후 토큰을 발급받고, getUpdates로 chat_id를 확인하세요."
        )
    return token, chat_id


def _call(method: str, token: str, **kwargs) -> dict:
    url = BASE_URL.format(token=token, method=method)
    try:
        resp = requests.post(url, timeout=30, **kwargs)
    except requests.RequestException as e:
        raise TelegramNotifyError(f"텔레그램 API 네트워크 오류({method}): {e}") from e

    try:
        payload = resp.json()
    except ValueError as e:
        raise TelegramNotifyError(
            f"텔레그램 API 응답이 JSON이 아닙니다({method}, HTTP {resp.status_code}): {resp.text[:300]}"
        ) from e

    if not payload.get("ok"):
        raise TelegramNotifyError(
            f"텔레그램 API 오류({method}, HTTP {resp.status_code}): "
            f"code={payload.get('error_code')}, {payload.get('description')}"
        )
    return payload


def send_text(message: str, token: str | None = None, chat_id: str | None = None) -> dict:
    """일반 텍스트 메시지 전송(4096자 제한, 초과 시 텔레그램이 거부하므로 호출자가 분할)."""
    tok, cid = (token, chat_id) if token and chat_id else _credentials()
    return _call("sendMessage", tok, data={"chat_id": cid, "text": message, "parse_mode": "HTML"})


def send_pdf_report(
    pdf_path: str | Path,
    caption: str | None = None,
    token: str | None = None,
    chat_id: str | None = None,
) -> dict:
    """PDF 리포트 파일을 문서로 전송한다. caption이 1024자를 넘으면 캡션은 잘라서
    보내고, 전체 내용은 별도 send_text로 나눠 보내는 방식을 권장(이 함수는 자르기만 함,
    분할 전송은 파이프라인 오케스트레이터에서 처리)."""
    tok, cid = (token, chat_id) if token and chat_id else _credentials()
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise TelegramNotifyError(f"PDF 파일을 찾을 수 없습니다: {pdf_path}")

    truncated_caption = None
    if caption:
        truncated_caption = (
            caption if len(caption) <= TELEGRAM_CAPTION_LIMIT
            else caption[: TELEGRAM_CAPTION_LIMIT - 3] + "..."
        )

    with open(pdf_path, "rb") as f:
        files = {"document": (pdf_path.name, f, "application/pdf")}
        data = {"chat_id": cid}
        if truncated_caption:
            data["caption"] = truncated_caption
        return _call("sendDocument", tok, data=data, files=files)


if __name__ == "__main__":
    import json

    print("=== 텔레그램 Bot API 연결성 진단 ===")
    try:
        token, chat_id = _credentials()
    except TelegramNotifyError as e:
        print(f"자격증명 미설정(예상됨): {e}")
        print(
            "\nTELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 미설정 — 라이브 테스트는 건너뜁니다."
        )
    else:
        print("=== 라이브 테스트: getMe (봇 정보 확인) ===")
        info = _call("getMe", token)
        print(json.dumps(info, ensure_ascii=False, indent=2))

        print("\n=== 라이브 테스트: 텍스트 메시지 전송 ===")
        result = send_text(
            "[quality-stock-daily] 파이프라인 연결 테스트 메시지입니다.",
            token=token, chat_id=chat_id,
        )
        print(f"전송 성공: message_id={result['result']['message_id']}")
