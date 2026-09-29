"""
models.py
v3 스코어카드(weights.yaml) 계산에 쓰이는 공통 데이터 구조.

원칙(사용자 표준 규칙 준수):
- 실제로 조사 불가능하거나 아직 확보하지 못한 값은 None으로 둔다. 0이나 임의의
  추정치로 채우지 않는다 — 점수 계산 함수들은 None을 만나면 해당 컴포넌트를
  "데이터 없음"으로 건너뛰고 그 사실을 결과에 명시한다(조용히 0점 처리하지 않음).
- 정성평가 항목(경제적 해자, IFRS18 MPM 공식화, 거시·사이클)은 이 모듈이 자체
  판단하지 않는다. Gemini 내러티브 생성 단계(추후 구현) 또는 사람이 근거(evidence)
  텍스트와 함께 QualitativeScore로 미리 채워 넣어야 하며, 근거 없는 정성 점수는
  거부한다(아래 QualitativeScore.__post_init__ 참고).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class QualitativeScore:
    """정성평가 결과 — 반드시 근거(evidence)를 동반해야 한다."""
    points_awarded: float
    max_points: float
    evidence: str  # 왜 이 점수인지 사실 근거(시장점유율, 공시 문구 등)

    def __post_init__(self):
        if not self.evidence or not self.evidence.strip():
            raise ValueError(
                "정성평가 점수는 반드시 근거(evidence)와 함께 제공되어야 합니다 "
                "— 근거 없는 정성 점수는 임의 해석으로 간주되어 거부됩니다."
            )
        if self.points_awarded < 0 or self.points_awarded > self.max_points:
            raise ValueError(
                f"points_awarded({self.points_awarded})는 0~{self.max_points} 범위여야 합니다."
            )


@dataclass
class RawFinancials:
    """8블록 스코어카드 계산에 필요한 원천 데이터. 수집 모듈(krx/dart/us_collector)의
    출력을 이 형태로 매핑해서 scorecard.compute_score()에 전달한다."""

    ticker: str
    company_name: str
    market: str  # "KR" | "US"
    as_of_date: str

    # --- A블록: 수익성·해자 (22점) ---
    roe_5y_pct: list[float] | None = None          # 최근 5개년 ROE(%), 최신이 마지막
    industry_roe_median_pct: float | None = None   # 업종 중앙값(있으면 Z-score, 없으면 절대컷 대체)
    roic_pct: float | None = None
    wacc_pct: float | None = None
    wacc_assumptions_note: str | None = None        # "표준 가정 사용" 등 명시 필수
    moat_qual: QualitativeScore | None = None        # 경제적 해자 정성평가 (외부 입력)

    # --- B블록: 이익의질·회계투명성 (9점) ---
    ocf_5y: list[float] | None = None                # 최근 5개년 영업현금흐름
    ni_5y: list[float] | None = None                 # 최근 5개년 순이익 (같은 연도 순서)
    total_assets_latest: float | None = None          # Sloan 발생액비율 분모
    ifrs18_segment_disclosed: bool | None = None      # 부문별 분리공시 여부(사실)
    ifrs18_mpm_qual: QualitativeScore | None = None   # MPM 공식화 정성평가 (외부 입력)

    # --- C블록: 재무완충력·이익방어력 (14점) ---
    retained_earnings_series: list[tuple[str, float]] | None = None  # [(연도, 금액), ...] 10년+ 권장
    debt_ratio_pct: float | None = None               # 부채비율(부채/자본totalxaidx100)
    altman_z: float | None = None

    # --- D블록: 주가방어력 (12점, 게이트와 공유) ---
    stress_test_result: dict | None = None            # collector의 stress_test_gate() 반환값 그대로

    # --- E블록: 추세·성장 (7점) ---
    revenue_cagr_3y_pct: float | None = None
    op_income_cagr_3y_pct: float | None = None

    # --- F블록: 가격 (18점) ---
    per: float | None = None
    pbr: float | None = None
    ev_ebit: float | None = None
    industry_per_median: float | None = None
    industry_pbr_median: float | None = None

    # --- G블록: 주주환원·지배구조 (10점) ---
    dividend_total: float | None = None
    net_buyback_amount: float | None = None
    net_debt_repayment: float | None = None
    market_cap: float | None = None
    treasury_retirement_ratio_pct: float | None = None  # 발행주식 대비 소각비율(%)
    commercial_code_benefit_qual: QualitativeScore | None = None  # 상법개정 수혜(업종) 정성평가 (외부 입력, 2026-09-30 신설)

    # --- H블록: 거시·사이클·재료 (8점, 전량 정성) ---
    macro_qual: QualitativeScore | None = None

    # --- 안전마진 게이트 ---
    margin_of_safety_result: dict | None = None       # {"mos_bear_pct":.., "mos_base_pct":.., "mos_bull_pct":.., "passed":bool, ...}


@dataclass
class ComponentResult:
    name: str
    points_awarded: float | None   # None = 데이터 없어 채점 불가(0점 아님!)
    max_points: float
    note: str


@dataclass
class BlockResult:
    block_key: str
    name: str
    max_points: float
    components: list[ComponentResult] = field(default_factory=list)

    @property
    def points_awarded(self) -> float:
        return sum(c.points_awarded for c in self.components if c.points_awarded is not None)

    @property
    def points_possible_given_data(self) -> float:
        """실제로 채점 가능했던(데이터가 있었던) 컴포넌트들의 배점 합 —
        데이터 결측으로 인한 '자동 감점'과 '진짜 저평가로 인한 감점'을 구분하기 위함."""
        return sum(c.max_points for c in self.components if c.points_awarded is not None)

    @property
    def has_missing_data(self) -> bool:
        return any(c.points_awarded is None for c in self.components)


@dataclass
class GateResult:
    gate_key: str
    passed: bool | None  # None = 데이터 부족으로 판정 불가
    detail: dict
    reason: str


@dataclass
class ScoreReport:
    ticker: str
    company_name: str
    market: str
    as_of_date: str
    blocks: list[BlockResult]
    gates: list[GateResult]

    @property
    def total_score(self) -> float:
        return sum(b.points_awarded for b in self.blocks)

    @property
    def total_possible(self) -> float:
        return sum(b.max_points for b in self.blocks)

    @property
    def all_gates_passed(self) -> bool:
        return all(g.passed is True for g in self.gates)

    @property
    def final_grade(self) -> str:
        """final_verdict_logic(weights.yaml) 구현:
        게이트 중 하나라도 미통과(또는 판정불가)면 무조건 '관찰'."""
        if not self.all_gates_passed:
            return "관찰"
        score = self.total_score
        if score >= 90:
            return "최상급우량주"
        if score >= 80:
            return "우량주"
        if score >= 70:
            return "관찰대상"
        return "미달"

    def gate_failure_reasons(self) -> list[str]:
        return [f"{g.gate_key}: {g.reason}" for g in self.gates if g.passed is not True]
