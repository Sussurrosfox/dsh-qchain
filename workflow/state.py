"""state.py — Strongly-typed state definition for QChain (题链) LangGraph Pipeline.
"""

from typing import TypedDict, List, Dict, Any, Optional, Literal


class GateIssue(TypedDict, total=False):
    category: str
    severity: Literal["HIGH_RISK", "WARN", "INFO"]
    message: str
    hash: Optional[str]
    file: Optional[str]
    line: Optional[int]


class GateReport(TypedDict, total=False):
    gate_name: str
    status: Literal["PASS", "REVIEW", "REJECT", "ERROR"]
    passed: bool
    issues: List[GateIssue]
    raw_report: Dict[str, Any]
    exit_code: int
    report_path: Optional[str]
    report_generated_at: Optional[str]
    report_is_fresh: bool
    run_id: Optional[str]


class ShrinkCoursewareState(TypedDict, total=False):
    # --- Context & Environment ---
    project_root: str
    self_dir: str
    input_path: str
    run_id: str
    stage: Literal[
        "INIT",
        "PHASE0_BREAKPOINT",
        "T0_INITIALIZED",
        "T1_MAPPING_PENDING",
        "T1_MAPPED",
        "T2_SECTIONS_PENDING",
        "T2_SECTIONS_STAGED",
        "T2_ASSEMBLED",
        "T2_SHUFFLED",
        "T3_CHECKED",
        "T3_BUILT",
        "T3_COMPARED",
        "T4_ACCEPTANCE_PENDING",
        "T4_ACCEPTED",
        "T4_DELIVERED",
        "REPAIR_LOOP",
        "HALTED",
        "ABORTED"
    ]

    # --- Phase 0: Human Confirmation (Interrupt) ---
    user_spec: Optional[Dict[str, Any]]
    human_interrupted: bool
    phase0_confirmed: bool  # 真人确认+令牌校验通过后置 True；否则断点强制挂起

    # --- T0: Project Scaffolding & Run Contract ---
    scaffold_initialized: bool
    config_yaml: Optional[Dict[str, Any]]

    # --- T1: Mapping & Materials Index ---
    source_map_file: Optional[str]
    dependency_map_file: Optional[str]
    coverage_map_file: Optional[str]
    materials_json_file: Optional[str]
    mapping_frozen: bool
    mapping_gate: GateReport

    # --- T2: Sections & pdstate Attempt Contract ---
    expected_sections: List[str]
    active_section: Optional[str]
    section_attempts: Dict[str, int]
    section_statuses: Dict[str, str]
    staged_sections: Dict[str, str]
    assembled_main_tex: Optional[str]
    t2_gate: GateReport
    shuffle_gate: GateReport
    shuffle_passed: bool

    # --- Cognitive Agent Dispatch ---
    pending_dispatch: Optional[Dict[str, Any]]
    dispatch_results: Dict[str, Any]
    worker_available: bool

    # --- Output Format ---
    output_format: Optional[Literal["both", "pdf", "html"]]

    # --- T3: Technical Quality Gates & Build ---
    pedagogy_gate: GateReport
    check_numbering_gate: GateReport
    dialogue_gate: GateReport
    budget_gate: GateReport
    build_gate: GateReport
    compare_gate: GateReport
    build_passed: bool

    # --- T4: Review Pack, Acceptance & Delivery ---
    review_pack_ready: bool
    acceptance_gate: GateReport
    acceptance_rounds: int
    deliver_gate: GateReport
    delivery_manifest: Optional[Dict[str, Any]]
    promoted: bool
    delivery_result: Dict[str, Any]

    # --- Flow Control & Diagnostics ---
    routing_action: Literal["PROCEED", "REPAIR_SECTION", "REPLAN", "REDISPATCH_ACCEPTANCE", "HALT"]
    repair_target: Optional[str]
    repair_reason: Optional[str]
    error_message: Optional[str]
    retry_count: int
    replan_count: int  # T1 映射重规划（REPLAN）累计次数，>3 熔断 HALT

    # --- Execution Trace ---
    history: List[str]
