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
        "T1_SOURCE_AUDITED",
        "T1_DEPENDENCY_PLANNED",
        "T1_COVERAGE_PLANNED",
        "T1_MAPPED",
        "T2_SECTIONS_PENDING",
        "T2_SECTIONS_RUNNING",
        "T2_SECTIONS_STAGED",
        "T2_CHECK_PENDING",
        "T2_CHECK_RUNNING",
        "T2_ASSEMBLED",
        "T2_SHUFFLED",
        "T3_CHECKED",
        "T3_BUILT",
        "T3_COMPARED",
        "T4_REVIEW_PENDING",
        "T4_REVIEW_RUNNING",
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

    # --- 任务1: Work-set Selection (audit §17 任务1 / §12.4) ---
    # run_scope: full(全量) | focused(只做目标节) | edit(只重生成已存在节)
    # target_sections/target_qids：Phase 0 规范化后的 list[str]（配置层是
    # 逗号分隔字符串；非法 run_scope 在 Phase 0 即 HALT，不静默回退 full）
    run_scope: str
    target_sections: List[str]
    target_qids: List[str]

    # --- F0: Frozen Contracts (source-manifest / intent-contract) ---
    # audit §4.1/§4.2：Phase 0 冻结的输入清单与目标合同（磁盘契约镜像，
    # 权威副本来位 <self_dir>/.pd/source-manifest.json 与 intent-contract.json）
    source_manifest: Optional[Dict[str, Any]]
    intent_contract: Optional[Dict[str, Any]]
    source_manifest_digest: str  # manifest 自摘要（cache key / gate 输入）
    contract_digest: str         # intent contract 自摘要
    content_policy: str          # faithful(默认) | authoring
    source_files: List[str]      # 规范化后的 include=true 原材料列表
    excluded_sources: List[str]  # 规范化后的排除项列表（登记而非消失）
    scope_change: Optional[Dict[str, Any]]  # 范围漂移人工确认点（F2/F3 写入）

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
    # §5.1 三角色 round 台账：{stage: input_digest}。verify 节点据此分键
    # 存入派生物缓存；失败时整链进入 dispatch_blocklist。
    t1_round_digests: Dict[str, str]

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

    # --- P0: Dispatch Observability & Hard Contract ---
    dispatch_plan: Optional[Dict[str, Any]]  # 本轮 batch 派发计划（batch_id/sections/requests/max_concurrency/...）
    batch_id: Optional[str]  # 当前派发批次 id
    # 缓存黑名单（audit §10）：已知产生不合格产物的 input_digest 集合。
    # REPLAN/修复回路不得再命中这些 digest 的派生物缓存（否则反复还原
    # 同一份不合格产物直至熔断，重派永远不发生）。
    dispatch_blocklist: List[str]
    dispatch_metrics: Dict[str, int]  # 计数器 submitted/completed/ok/failed/retried/max_in_flight/deferred/reused
    dispatch_history: List[Dict[str, Any]]  # 每轮 batch 归档摘要
    agent_dispatch_profile: str  # safe | fast | budget（默认 safe）
    # 任务19（C12）：派发契约。dispatch_contract_version 缺失=1（旧检查点/旧契约），
    # 新 run 冻结为 2；dispatch_config 是 workflow/dispatch_policy.py 的解析结果
    # （档位/配置原值/生效来源/执行器种类/有效并发），批次计划据此派生。
    dispatch_contract_version: int
    dispatch_config: Dict[str, Any]
    manual_interrupted_batch: Optional[str]  # manual 模式已中断过的 batch_id（防二次 interrupt，幂等回边依据）
    # 派发模式：parallel（活池真实并发）| manual（人工/Antigravity 批次）
    # | reused（任务6：全部 scope 复用、零派发直收）
    dispatch_mode: str

    # --- §6: Semantic Checker Round (Writer → Checker → Gate) ---
    # 语义审读轮独立于写作轮：审读结果（.pd/semantic/<sec>.json）是串行
    # 提交的硬前置。semantic_repair_target 非空表示「缺结果/摘要过期/
    # schema 非法，须补派审读」，图路由据此改走 t2_plan_check 而非写作者返修。
    semantic_repair_target: Optional[str]
    semantic_gate: GateReport  # check_semantic.py 汇总报告（交付门禁必过项）
    semantic_check_rounds: int  # 语义审读轮累计次数（manual 模式防无限中断，>3 熔断）

    # --- Output Format ---
    output_format: Optional[Literal["both", "pdf", "html"]]

    # --- T3: Technical Quality Gates & Build ---
    pedagogy_gate: GateReport
    check_numbering_gate: GateReport
    dialogue_gate: GateReport
    # §7 scope_gate：Phase 0 冻结合约 vs 生效配置零漂移（check_scope.py）
    scope_gate: GateReport
    fidelity_gate: GateReport
    # §7 provenance_gate：writer attempt → checker → qid 血缘链
    # （check_provenance.py）
    provenance_gate: GateReport
    budget_gate: GateReport
    build_gate: GateReport
    # 渲染质量门禁（check_render.py，audit §19）：HTML 乱码/源码泄漏/数学段
    # 损坏/选项汉字丢失、PDF 缺字/空选项组的渲染级硬门禁，由 node_t3_build
    # 在 build.py 之后执行（产物就绪才可查）。
    render_gate: GateReport
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

    # --- 卡 06 / C3：有限返修与无进展熔断（全部持久化进 checkpoint）---
    # 内容修复轮 repair ≠ 运输 attempt ≠ 覆盖重规划 replan；旧 state 缺这些键
    # 一律按空/0 处理（见 workflow/repair_budget.py）。dispatch 返回 ok 不清零；
    # 仅 *_last_* 的“本次未解决标记”在相关门禁 PASS 时解除，计数永不重置。
    repair_attempts_by_scope: Dict[str, int]  # 每 scope 已安排的返修次数
    repair_attempts_total: int  # 全 run 累计返修次数（永不重置）
    repair_last_fingerprint_by_scope: Dict[str, str]  # 上次返修时的 issue 指纹
    repair_last_digest_by_scope: Dict[str, Optional[str]]  # 上次返修前的产物摘要
    repair_attempt_log: List[Dict[str, Any]]  # attempt 来源（dispatch_id/指纹/输入摘要）
    repair_halt: Optional[Dict[str, Any]]  # HALT 证据：原因/未解决 issue/最后产物/attempt 来源
    repair_issues: Optional[List[Dict[str, Any]]]  # 可选：C2 结构化 issue（指纹用）
    repair_per_scope_limit: Optional[int]  # 可选显式上限覆盖（缺省按 profile）
    repair_total_limit: Optional[int]
    execution_profile: Optional[str]  # legacy | faithful_fast（任务10 接入；缺省=legacy）

    # --- 卡 10 / C1：执行契约冻结元数据（随 Phase 0 确认一次性冻结）---
    # 与 content_policy 彻底分离：content_policy 描述内容忠实度，graph_version
    # 决定编译哪张 StateGraph。旧检查点没有这些键 -> 一律按 legacy-v1 解释
    # （见 workflow/execution_policy.py），绝不静默换图、绝不回写旧 SQLite。
    # 这些字段只进 state 与 intent-contract.json，**不进 TeX 版式宏**。
    execution_schema_version: int  # 冻结元数据版本（旧记录缺失=1，新 run=2）
    graph_version: str  # legacy-v1 | faithful-fast-v1
    policy_digest: str  # sha256:<hex>，覆盖 content_policy+执行配置+内容相关键
    execution_config: Dict[str, Any]  # 规范化后的有效执行配置（C1 八个键）

    # --- 卡 15 / C6：构建前就绪（prebuild）与终审（final）两级门禁 ---
    # prebuild_ready_sections: 已按 pdstate 契约提交（produced）并通过结构/来源
    #   身份检查的逐节内容摘要 {sec: sha256}。它只说明「可以进入构建前置检查」，
    #   **不**代表已审读；作者循环据此判断剩余节，避免把已产出的节反复重写。
    # gate_phase: None(legacy) | "prebuild" | "final"。同一 t3_checks 节点按此
    #   分层：prebuild 只跑确定性检查（semantic 记 pending_review），final 才跑
    #   全量 semantic/provenance/acceptance 并在通过后逐节 mark checked。
    prebuild_ready_sections: Dict[str, str]
    gate_phase: Optional[str]

    # --- 卡 15：合并终审（faithful_fast 的 T4 批次审读结论）---
    combined_review_conclusion: Optional[str]  # PASS | REVISE | NEEDS_EVIDENCE
    combined_review_scopes: Optional[List[str]]  # 本轮/待复查的 scope 列表
    combined_review_problems: Optional[List[Dict[str, Any]]]
    combined_review_artifacts: Dict[str, str]  # scope -> 终审主产物相对路径

    # --- Execution Trace ---
    history: List[str]
