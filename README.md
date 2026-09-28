# quality-stock-daily

한국(KR) 1종목 + 미국(US) 1종목의 우량주 스코어카드(v3, 8블록 100점 + 2게이트)를
매일 계산해 PDF 리포트로 만들고 텔레그램으로 전송하는 자동화 파이프라인.

## 방법론

`config/weights.yaml`에 전체 스코어카드 정의가 있다. 원본 아이디어는
"실전경제학 리서치 Vol.1"의 10항목 100점 스코어카드이며, v3는 그중 서로
겹치는 항목은 계승하고, 스트레스테스트(D블록)·안전마진(게이트)·거시(H블록)를
신설 추가했다. 두 게이트(`stress_test_gate`, `margin_of_safety_gate`) 중
하나라도 미통과면 8블록 총점과 무관하게 최종 등급이 "관찰"로 강등된다.

## 현재 구현 상태 (2026-09-28)

| 구성요소 | 상태 | 비고 |
|---|---|---|
| `src/collectors/krx_collector.py` | 구조 완성, 실측 미검증 | KRX Open API 기반. `KRX_AUTH_KEY` 필요 + 서비스별 개별 승인 필요 |
| `src/collectors/dart_collector.py` | 구조 완성, 실측 미검증 | OpenDART 기반. `DART_API_KEY` 필요 |
| `src/collectors/us_collector.py` | **완료, MSFT 실측 검증됨** | yfinance 기반, 키 불필요 |
| `src/scoring/` (8블록+2게이트) | **완료, 삼성전자 실측 데이터로 검증됨** | `tests/test_scorecard_samsung.py` |
| `src/report/` (PDF 생성) | **완료, 실제 PDF 생성 검증됨** | xhtml2pdf + 번들 NanumGothic(`assets/fonts/`) |
| `src/notify/telegram_notifier.py` | 구조 완성, 실측 미검증 | 봇 토큰 필요 |
| `src/selection.py` (종목 선정 로직) | **완료, 로직 검증됨** | 게이트통과+최고점, 쿨다운, 전원미통과 폴백 — `tests/test_selection.py` |
| `config/watchlist.yaml` (후보군) | **초안, 리오님 검토 대기** | KR 23종목 + US 24종목 큐레이션 초안 — 확정 아님 |
| `src/main.py` — `build_us_report()` | **완료, US 24종목 전체 라이브 검증됨** | 키 불필요(yfinance). `select_daily_pick()`까지 실제 연동 테스트 완료 |
| `src/main.py` — `build_kr_report()` | 작성됨, 실측 미검증 | `KRX_AUTH_KEY`+`DART_API_KEY` 필요. ROE 지표명 매칭이 가장 확인 필요 |
| `.github/workflows/daily-report.yml` | 작성됨, 아직 미실행 가능 | KR 키 확보 전까지는 US만 픽 가능. pick_history.json 자동 커밋 포함 |

## 남은 작업 (다음 단계)

1. **KR 키 발급 후 `build_kr_report()` 라이브 검증** — 코드는 US와 동일한
   설계로 이미 작성돼 있다. 실제 키로 1회 실행해서 `dart_collector`의
   ROE 지표명(idx_nm) 매칭이 맞는지 확인하고 필요시 수정.
2. **`config/watchlist.yaml` 후보군 최종 검토**: 지금 리스트는 제가 초안으로
   큐레이션한 것 — 종목 추가/제외 의견 반영 필요.
3. 세 가지 외부 키 발급(전부 사용자 본인 명의로 무료 발급):
   - `KRX_AUTH_KEY` — openapi.krx.co.kr (+ 서비스별 API 이용신청 승인)
   - `DART_API_KEY` — opendart.fss.or.kr
   - `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` — @BotFather
4. 위 키들을 GitHub 저장소 Secrets로 등록.
5. **정성평가 3항목(경제적 해자·IFRS18 MPM·거시/사이클, 총 17.5/100점) 보강**:
   지금은 종목별 사실 근거가 필요해 자동 채점에서 의도적으로 비워두고
   N/A 처리 중 — Gemini 등으로 리서치 기반 정성평가를 채우는 단계가 필요하다.

## 종목 선정 방식 (2026-09-28 확정)

매일 `config/watchlist.yaml`의 후보군 전체를 v3 스코어카드로 채점 →
`stress_test_gate`·`margin_of_safety_gate`를 모두 통과한 종목 중 최고점을
그날의 픽으로 선정. 최근 10일(기본값, `selection.cooldown_days`로 조정 가능)
내 이미 픽으로 나온 종목은 후보에서 제외. 게이트 통과 종목이 하나도 없으면
픽 없이 "오늘은 통과 종목 없음" + 점수 상위 3개를 참고용으로만 병기(억지로
픽을 만들지 않음). 로직은 `src/selection.py`, 기록은 `data/pick_history.json`.

## 로컬 테스트

```bash
pip install -r requirements.txt
python3 tests/test_scorecard_samsung.py   # 스코어카드 엔진 회귀 테스트
python3 tests/test_pdf_generation.py      # PDF 생성 테스트
python3 tests/test_selection.py           # 종목 선정 로직 단위테스트
python3 src/collectors/us_collector.py    # yfinance 라이브 테스트(키 불필요)
```
