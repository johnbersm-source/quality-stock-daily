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
| `src/collectors/krx_collector.py` | **완료, 실측 검증됨(2026-09-29)** | KRX Open API 기반. 삼성전자 D블록 스트레스테스트(지수+개별종목 시세) 실제 값 확인, 4개 서비스 승인 완료 |
| `src/collectors/dart_collector.py` | 구조 완성, 실측 미검증 | OpenDART 기반. `DART_API_KEY` 필요 |
| `src/collectors/us_collector.py` | **완료, MSFT 실측 검증됨** | yfinance 기반, 키 불필요 |
| `src/scoring/` (8블록+2게이트) | **완료, 삼성전자 실측 데이터로 검증됨** | `tests/test_scorecard_samsung.py` |
| `src/report/` (PDF 생성) | **완료, 실제 PDF 생성 검증됨** | xhtml2pdf + 번들 NanumGothic(`assets/fonts/`) |
| `src/notify/telegram_notifier.py` | **완료, 실측 검증됨(2026-09-29)** | 실제 테스트 메시지 전송·수신 확인 완료 |
| `src/selection.py` (종목 선정 로직) | **완료, 로직 검증됨** | 게이트통과+최고점, 쿨다운, 전원미통과 폴백 — `tests/test_selection.py` |
| `src/universe.py` (후보군 동적 생성) | **완료, KR/US 모두 실측 검증됨(2026-09-29)** | 매 실행마다 그 시점 기준으로 후보군 재계산(아래 "후보군 생성 방식" 참고) — KR 350개, US 503개 실제 조회 확인. `tests/test_universe.py` |
| `src/main.py` — `build_us_report()` | **완료, US 다수 후보 전체 라이브 검증됨** | 키 불필요(yfinance). `select_daily_pick()`까지 실제 연동 테스트 완료 |
| `src/main.py` — `build_kr_report()` | **완료, 8블록 전부 실측 검증됨(2026-09-29)** | 삼성전자로 컴포넌트별 상세까지 확인 — B(이익의질)·G(주주환원) 정상 작동, F(가격)는 PER=34.92/PBR=3.62/EV·EBIT=37.87 실측 계산됐고 고평가 구간이라 0점(정직한 결과, 버그 아님). 이익잉여금 10년치·Altman Z·net_debt_repayment만 데이터 소스 미비로 N/A로 남음(추후 보강 가능) |
| `.github/workflows/daily-report.yml` | 작성됨, 아직 미실행 | `DART_API_KEY` 등록 후 1차 실행 가능. pick_history.json 자동 커밋 포함 |

## 후보군 생성 방식 (2026-09-28, 정적 리스트 → 동적 생성으로 전환)

**이전에는 `config/watchlist.yaml`에 KR 23종목 + US 24종목을 손으로 큐레이션해
고정 리스트로 박아뒀었다.** 리오님 지시로 이 방식을 폐기했다: *"후보종목을
미리 선정해서 하는게 아니라 매일 보고서를 만들어 보낼때 그 시간 기준으로
해당되는 기업을 선정해서 보고서를 맞춰서 작성해야해."* 이제 `src/universe.py`가
**매 실행마다 그 시점 기준으로 후보군을 새로 조회**한다:

- **KR**: 원래 목표는 공식 KOSPI200+KOSDAQ150 지수 구성종목이었으나, (1) 공식
  KRX Open API(`openapi.krx.co.kr`, 이미 승인받은 `KRX_AUTH_KEY`)에는 지수
  구성종목 조회 기능 자체가 없고, (2) 실제 구성종목은 `data.krx.co.kr`의
  비공식 내부 API에서만 나오는데 이 경로는 2024-12-27부터 로그인(KRX_ID/PW)이
  의무화됐다(봇탐지 목적) — 매일 GitHub Actions에서 개인 계정 로그인을
  자동화하면 계정 제한/정지 위험이 있어(`krx_collector.py`가 애초에 이 문제
  때문에 pykrx 방식을 버리고 공식 API로 재작성된 전례와 동일한 이유), 대신
  **코스피 시가총액 상위 200 + 코스닥 시가총액 상위 150**을 대리 지표(proxy)로
  쓰기로 확정(2026-09-28, 리오님과 합의). 이미 승인된 공식 API만 쓰므로 추가
  키·로그인 불필요.
- **US**: S&P500 실제 구성종목(위키피디아 "List of S&P 500 companies" 공개
  표를 스크래핑, 대리 지표 아님) 전체 ~500종목. 이 세션에서 503개 확인,
  BRK.B → BRK-B 등 yfinance 호환 티커 정규화까지 실측 검증 완료.

파라미터(`top_n_kospi`/`top_n_kosdaq`)는 `config/pipeline.yaml`에서 조정 가능.

### ⚠️ 규모 확대에 따른 실행시간/API 호출량 리스크 (미해결, 1차 실행 후 재점검 필요)

후보군이 47개(KR 23+US 24)에서 **KR ~350개 + US ~500개 = 총 ~850개**로 크게
늘었다.
- DART API(10,000건/일 한도): KR 350종목 × 종목당 ~6회 ≈ 2,100건 — 여유 있음.
- yfinance(US, 비공식 엔드포인트): 이미 24종목 테스트에서도 429(rate limit)를
  겪은 적이 있어, 500종목 규모에서는 실행시간·안정성이 검증되지 않았다.
- GitHub Actions: `timeout-minutes`를 20→90으로 늘려뒀지만 실측 근거는 없다.
  또한 private 저장소는 플랜별 월간 Actions 분 한도가 있어, 매일 장시간
  실행이 그 한도를 얼마나 소모하는지도 1차 실행 전까지는 알 수 없다.

**결론: 이 규모(850종목)로 1회 실제 GitHub Actions 실행을 해보고, 실행시간·
성공률·Actions 분 소모량을 같이 확인한 뒤 필요시 사전 스크리닝(예: 전체 종목
중 일부만 상세 스코어링)을 추가하는 게 다음 단계로 필요할 것으로 보인다.
지금 단계에서 미리 스크리닝을 넣지는 않았다(실측 없이 임의로 걸러내는 건
지어내는 것과 같은 문제라 판단).**

## 남은 작업 (다음 단계)

1. ~~KR 키 발급 및 `krx_collector.py`/`src/universe.py` KR 경로 라이브 검증~~
   **완료 (2026-09-29)** — `KRX_AUTH_KEY` 4개 서비스(코스피/코스닥 주식
   매매정보, 코스피/코스닥 지수 시세) 승인 확인, 삼성전자 D블록
   스트레스테스트·시세·시가총액 상위 N 스냅샷 모두 실측 성공.
2. ~~텔레그램 봇 발급 및 `telegram_notifier.py` 라이브 검증~~
   **완료 (2026-09-29)** — `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` 등록,
   실제 테스트 메시지 전송·수신 확인.
3. ~~`DART_API_KEY` 발급 및 GitHub Secrets 등록~~ **완료 (2026-09-29)**.
4. ~~`build_kr_report()`의 KR 스코어카드 재검증~~ **완료 (2026-09-29)** —
   `bsns_year` 버그 수정, B(이익의질)·F(가격)·G(주주환원) 구조적 0점 문제
   보강, `get_treasury_stock()` 엔드포인트 오타(`tesstkAcqusDspsSttus`→
   `tesstkAcqsDspsSttus`) 수정까지 전부 삼성전자 실측으로 컴포넌트 단위까지
   확인 완료. 남은 소소한 미보강 항목(이익잉여금 10년치, Altman Z,
   net_debt_repayment)은 데이터 소스가 추가로 필요해 다음 기회로 미룸.
5. **1차 실제 GitHub Actions 실행으로 850종목 규모의 실행시간/API 호출량 확인**
   (위 리스크 섹션 참고) — 필요시 사전 스크리닝 설계.
6. **정성평가 3항목(경제적 해자·IFRS18 MPM·거시/사이클, 총 17.5/100점) 보강**:
   지금은 종목별 사실 근거가 필요해 자동 채점에서 의도적으로 비워두고
   N/A 처리 중 — Gemini 등으로 리서치 기반 정성평가를 채우는 단계가 필요하다.

## 종목 선정 방식 (2026-09-28 확정)

매일 `src/universe.py`가 그 시점 기준으로 동적 생성한 후보군 전체를 v3
스코어카드로 채점 → `stress_test_gate`·`margin_of_safety_gate`를 모두 통과한
종목 중 최고점을 그날의 픽으로 선정. 최근 10일(기본값,
`config/pipeline.yaml`의 `selection.cooldown_days`로 조정 가능) 내 이미
픽으로 나온 종목은 후보에서 제외. 게이트 통과 종목이 하나도 없으면 픽 없이
"오늘은 통과 종목 없음" + 점수 상위 3개를 참고용으로만 병기(억지로 픽을
만들지 않음). 로직은 `src/selection.py`, 기록은 `data/pick_history.json`.

## 로컬 테스트

```bash
pip install -r requirements.txt
python3 tests/test_scorecard_samsung.py   # 스코어카드 엔진 회귀 테스트
python3 tests/test_pdf_generation.py      # PDF 생성 테스트
python3 tests/test_selection.py           # 종목 선정 로직 단위테스트
python3 tests/test_universe.py            # 후보군 동적 생성 로직 단위테스트(목킹, 네트워크 불필요)
python3 src/collectors/us_collector.py    # yfinance 라이브 테스트(키 불필요) — S&P500 스크래핑 포함
python3 -m src.universe                   # 후보군 실측 생성 테스트(US는 키 불필요, KR은 KRX_AUTH_KEY 필요)
```
