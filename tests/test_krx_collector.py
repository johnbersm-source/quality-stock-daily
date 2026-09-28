"""
test_krx_collector.py
2026-09-29 실측(GitHub Actions 라이브 로그)으로 발견된 버그의 회귀테스트:
get_index_ohlcv()가 "코스피" 부분일치로 "코스피(외국주포함)"처럼 가격 필드가
빈 값인 부가 지수 행에 걸려서 죽던 문제. _fetch_day를 monkeypatch해 네트워크
없이 그 상황을 재현하고, 수정된 로직(정확일치 우선 → 부분일치 순서대로 →
빈 값이면 다음 후보)이 올바르게 동작하는지 확인한다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.collectors import krx_collector  # noqa: E402


def run_test():
    # --- 케이스 1: 정확일치 행이 뒤에 있어도(순서 무관) 제대로 찾아야 함,
    #     그리고 빈 값인 부분일치("코스피(외국주포함)")는 건너뛰어야 함 ---
    def fake_fetch_day_case1(service, bas_dd):
        return [
            {"IDX_NM": "코스피(외국주포함)", "CLSPRC_IDX": "", "OPNPRC_IDX": "",
             "HGPRC_IDX": "", "LWPRC_IDX": ""},
            {"IDX_NM": "코스피 200", "CLSPRC_IDX": "350.12", "OPNPRC_IDX": "349.00",
             "HGPRC_IDX": "351.00", "LWPRC_IDX": "348.50"},
            {"IDX_NM": "코스피", "CLSPRC_IDX": "2500.55", "OPNPRC_IDX": "2490.00",
             "HGPRC_IDX": "2510.00", "LWPRC_IDX": "2480.00"},
        ]

    orig = krx_collector._fetch_day
    krx_collector._fetch_day = fake_fetch_day_case1
    try:
        rows = krx_collector.get_index_ohlcv("코스피", "20260601", "20260601", market="KOSPI")
    finally:
        krx_collector._fetch_day = orig

    assert len(rows) == 1, f"영업일 1일치 1건이어야 하는데 {len(rows)}건"
    assert rows[0]["close"] == 2500.55, (
        f"정확일치 '코스피' 행(종가 2500.55)이 선택돼야 하는데 {rows[0]['close']}"
    )
    print(f"[케이스1 PASS] 정확일치 우선 + 빈값 행 스킵 확인: close={rows[0]['close']}")

    # --- 케이스 2: 정확일치 행 자체가 빈 값이면, 부분일치 후보 중 값 있는
    #     행으로 폴백해야 함 ---
    def fake_fetch_day_case2(service, bas_dd):
        return [
            {"IDX_NM": "코스피", "CLSPRC_IDX": "", "OPNPRC_IDX": "",
             "HGPRC_IDX": "", "LWPRC_IDX": ""},  # 정확일치인데 빈 값(실제 관측된 패턴)
            {"IDX_NM": "코스피 200 중소형주", "CLSPRC_IDX": "999.99", "OPNPRC_IDX": "1",
             "HGPRC_IDX": "2", "LWPRC_IDX": "0.5"},
        ]

    krx_collector._fetch_day = fake_fetch_day_case2
    try:
        rows2 = krx_collector.get_index_ohlcv("코스피", "20260601", "20260601", market="KOSPI")
    finally:
        krx_collector._fetch_day = orig

    assert len(rows2) == 1 and rows2[0]["close"] == 999.99, (
        f"정확일치 행이 빈 값이면 부분일치 폴백(999.99)으로 가야 하는데 {rows2}"
    )
    print(f"[케이스2 PASS] 정확일치 빈값 -> 부분일치 폴백 확인: close={rows2[0]['close']}")

    # --- 케이스 3: 매칭되는 행이 전혀 없으면(그리고 구간 전체가 그러면) 여전히
    #     ValueError를 내야 함(조용히 빈 리스트를 돌려주면 안 됨) ---
    def fake_fetch_day_case3(service, bas_dd):
        return [{"IDX_NM": "완전다른지수", "CLSPRC_IDX": "1.0"}]

    krx_collector._fetch_day = fake_fetch_day_case3
    try:
        raised = False
        try:
            krx_collector.get_index_ohlcv("코스피", "20260601", "20260601", market="KOSPI")
        except ValueError:
            raised = True
        assert raised, "매칭 행이 하나도 없으면 ValueError가 나야 함"
    finally:
        krx_collector._fetch_day = orig
    print("[케이스3 PASS] 매칭 없음 -> ValueError 확인")

    print("\nALL PASS")


if __name__ == "__main__":
    run_test()
