"""runner.py — CLI and API driver for QChain (题链) LangGraph Pipeline.
Supports executing the graph, Phase 0 human interrupts/resumes, and state inspection.
"""

import argparse
import json
import os
import sys
from typing import Dict, Any, Optional

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_CUR_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT_DIR = os.path.dirname(_CUR_DIR)
if _CUR_DIR not in sys.path:
    sys.path.insert(0, _CUR_DIR)
if _PARENT_DIR not in sys.path:
    sys.path.insert(0, _PARENT_DIR)

_SCRIPTS_DIR = os.path.abspath(os.path.join(_PARENT_DIR, "scripts"))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
import setup_self

from langgraph.types import Command

if __package__:
    from .graph import create_shrink_courseware_graph
    from .state import ShrinkCoursewareState
else:
    try:
        from workflow.graph import create_shrink_courseware_graph
        from workflow.state import ShrinkCoursewareState
    except ImportError:
        from graph import create_shrink_courseware_graph
        from state import ShrinkCoursewareState

DEFAULT_DB_NAME = "courseware_checkpoints.sqlite"
_SAVER_CACHE: Dict[str, Any] = {}


def default_checkpoint_db(project_root: str) -> str:
    return os.path.join(os.path.abspath(project_root), ".langgraph", DEFAULT_DB_NAME)


def get_checkpointer(db_path: Optional[str] = None):
    if not db_path:
        from langgraph.checkpoint.memory import MemorySaver
        saver = _SAVER_CACHE.get("memory")
        if saver is None:
            saver = MemorySaver()
            _SAVER_CACHE["memory"] = saver
        return saver

    key = os.path.abspath(db_path)
    saver = _SAVER_CACHE.get(key)
    if saver is None:
        if __package__:
            from .checkpoints import SqliteSaver
        else:
            try:
                from workflow.checkpoints import SqliteSaver
            except ImportError:
                from checkpoints import SqliteSaver
        saver = SqliteSaver(key)
        _SAVER_CACHE[key] = saver
    return saver


def get_graph(db_path: Optional[str] = None):
    if db_path:
        return create_shrink_courseware_graph(checkpointer=get_checkpointer(db_path))
    return create_shrink_courseware_graph()


def run_pipeline(
    project_root: str,
    input_path: Optional[str] = None,
    user_spec: Optional[Dict[str, Any]] = None,
    thread_id: str = "qchain_session",
    db_path: Optional[str] = None,
    reuse_thread: bool = False
) -> Dict[str, Any]:
    """Execute or advance the qchain courseware pipeline."""
    # 强制在输入文件所在处生成/锁定 self 文件夹，严禁向输入根目录污染文件
    target_input = input_path or project_root
    base_dir, self_dir = setup_self.resolve_self_dir(target_input)

    title = (user_spec or {}).get("main_title")
    profile = (user_spec or {}).get("delivery_profile", "self_study")
    output_format = (user_spec or {}).get("output_format", "both")
    lesson_minutes = (user_spec or {}).get("lesson_minutes")
    self_dir = setup_self.setup_self(
        input_path=target_input,
        title=title,
        profile=profile,
        output_format=output_format,
        lesson_minutes=lesson_minutes
    )

    graph = get_graph(db_path)
    config = {"configurable": {"thread_id": thread_id}}

    # A2：串线防护 —— 同一 thread 已有检查点时，拒绝直接复用（旧 user_spec/
    # 切片状态/门禁报告会与新输入串线，且 Phase 0 会被残留状态跳过）。
    if not reuse_thread:
        try:
            existing = graph.get_state(config)
            if existing and existing.values:
                raise SystemExit(
                    "[runner] 拒绝复用已有检查点的 thread_id=%r（旧 stage=%s）。\n"
                    "  - 若要从断点继续：请用 --resume；\n"
                    "  - 若要全新启动：请更换 --thread-id；\n"
                    "  - 若确需覆盖：显式加 --reuse-thread。"
                    % (thread_id, existing.values.get("stage"))
                )
        except SystemExit:
            raise
        except Exception:
            pass

    # 任务1：工作集字段随 user_spec 进入初始 state（Phase 0 会再次规范化，
    # 非法 run_scope 在断点确认时 HALT，不静默回退 full）。
    from .scope_selection import RUN_SCOPES, normalize_targets
    spec = user_spec or {}
    run_scope = str(spec.get("run_scope") or "full").strip()
    if run_scope not in RUN_SCOPES:
        run_scope = "full"  # 初始装载宽容；Phase 0 确认时严格校验并 HALT
    init_state: ShrinkCoursewareState = {
        "project_root": os.path.abspath(base_dir),
        "self_dir": os.path.abspath(self_dir),
        "input_path": os.path.abspath(input_path) if input_path else "",
        "stage": "INIT",
        "user_spec": user_spec,
        "run_scope": run_scope,
        "target_sections": normalize_targets(spec.get("target_sections")),
        "target_qids": normalize_targets(spec.get("target_qids")),
        "history": [f"Runner: 启动题链流水线 (工作区强制锁定于: {self_dir})..."]
    }

    result = graph.invoke(init_state, config=config)
    return result


def resume_pipeline(
    user_response: Dict[str, Any],
    thread_id: str = "qchain_session",
    db_path: Optional[str] = None,
    expected_self_dir: Optional[str] = None
) -> Dict[str, Any]:
    """Resume pipeline from a Phase 0 human interrupt."""
    graph = get_graph(db_path)
    config = {"configurable": {"thread_id": thread_id}}

    # A2：跨工作区串线防护 —— 恢复前比对检查点中的 self_dir 与本次推导值
    if expected_self_dir:
        try:
            snap = graph.get_state(config)
            saved = ((snap.values or {}).get("self_dir") or "") if snap else ""
            if saved and os.path.abspath(saved) != os.path.abspath(expected_self_dir):
                raise SystemExit(
                    "[runner] 拒绝恢复：检查点 thread_id=%r 绑定的 self_dir=%s "
                    "与本次推导的工作区 %s 不一致（疑似跨工作区串线）。"
                    % (thread_id, saved, expected_self_dir)
                )
        except SystemExit:
            raise
        except Exception:
            pass

    resume_cmd = Command(resume=user_response)
    result = graph.invoke(resume_cmd, config=config)
    return result


def main():
    parser = argparse.ArgumentParser(description="题链流水线 (LangGraph)")
    parser.add_argument("--project", default=".", help="工作区项目目录")
    parser.add_argument("--input", default="", help="原材料文件或目录")
    parser.add_argument("--run", action="store_true", help="启动流水线")
    parser.add_argument("--resume", action="store_true", help="从断点恢复")
    parser.add_argument("--title", help="学案主标题")
    parser.add_argument("--profile", choices=["classroom", "teaching", "self_study"], default=None,
                        help="交付模式：self_study(自主学习模式，默认，不设时长上限) | teaching/classroom(课堂教学模式，保留课时预算上限)")
    parser.add_argument("--format", choices=["both", "pdf", "html"], default=None,
                        help="输出格式：both(全部输出) | pdf(仅 PDF) | html(仅 HTML)"
                             "（缺省沿用检查点中已确认的值；均无则 both）")
    parser.add_argument("--lesson-minutes", type=int, default=None,
                        help="单课总预算时长(分钟，仅 teaching/classroom 模式生效，默认 90)")
    parser.add_argument("--thread-id", default="qchain_session", help="会话线程 ID")
    parser.add_argument("--checkpoint-db", help="自定义 SQLite 检查点数据库路径")
    parser.add_argument("--status", action="store_true", help="查询流水线状态与检查点")
    parser.add_argument("--visualize", action="store_true", help="打印 Mermaid 拓扑图")
    parser.add_argument("--token", default=None,
                        help="Phase 0 断点恢复令牌（--resume 时由真人用户提供；"
                             "令牌由流水线写入 .langgraph/phase0_token.txt，严禁 Agent 自行读取）")
    parser.add_argument("--reuse-thread", action="store_true",
                        help="允许 --run 复用已有检查点的 thread_id（默认拒绝，防状态串线）")

    args = parser.parse_args()

    # 根据输入文件或工程目录强制推导 base_dir 与 self_dir
    target_input = args.input or args.project
    base_dir, self_dir = setup_self.resolve_self_dir(target_input)
    project_root = os.path.abspath(base_dir)
    db_path = args.checkpoint_db or default_checkpoint_db(project_root)

    if args.status:
        graph = get_graph(db_path)
        config = {"configurable": {"thread_id": args.thread_id}}
        try:
            state_snapshot = graph.get_state(config)
            vals = state_snapshot.values or {}
        except Exception:
            vals = {}
            state_snapshot = None

        print("=" * 60)
        print(f"[*] 题链流水线状态: thread={args.thread_id}")
        print(f"  基准目录 (输入处): {base_dir}")
        print(f"  强制工作区:       {self_dir}")
        if vals:
            print(f"  当前阶段: {vals.get('stage', 'UNKNOWN')}")
            print(f"  路由动作: {vals.get('routing_action', 'NONE')}")
            print(f"  输出格式: {vals.get('output_format', 'both')}")
            if state_snapshot and state_snapshot.next:
                print(f"  待执行节点: {', '.join(state_snapshot.next)}")
            hist = vals.get("history", [])
            if hist:
                print(f"  最新进展: {hist[-1]}")
        else:
            print("  检查点记录: 当前会话暂无执行中的状态快照。")

        run_json = os.path.join(self_dir, ".pd", "run.json")
        if os.path.isfile(run_json):
            try:
                with open(run_json, "r", encoding="utf-8-sig") as f:
                    rj = json.load(f)
                    print(f"  工作区 Run ID: {rj.get('run_id')}")
                    slices = rj.get("slices", [])
                    if slices:
                        print(f"  切片规划 ({len(slices)} 个): {', '.join(slices)}")
            except Exception:
                pass
        print("=" * 60)
        return

    if args.visualize:
        graph = get_graph()
        print("\n" + graph.get_graph().draw_mermaid() + "\n")
        return

    if args.run:
        # 只收集显式提供的字段：缺省字段不在派发时静默落值，
        # 由 Phase 0 断点与检查点中已确认的值决定（BUG #4：旧实现
        # `if args.title or args.format` 会漏掉仅传 --profile 的情形，
        # 且默认值会把已确认的 self_study 静默改写为 classroom）。
        spec = {}
        if args.title:
            spec["main_title"] = args.title
        if args.profile:
            spec["delivery_profile"] = args.profile
        if args.format:
            spec["output_format"] = args.format
        if args.lesson_minutes is not None:
            spec["lesson_minutes"] = args.lesson_minutes
        print(f"[*] 启动流水线: project={project_root}, thread={args.thread_id}, "
              f"format={args.format or 'both'}")
        res = run_pipeline(
            project_root=project_root,
            input_path=args.input,
            user_spec=spec or None,
            thread_id=args.thread_id,
            db_path=db_path,
            reuse_thread=args.reuse_thread
        )
        print(f"[*] 执行阶段: {res.get('stage')}, 路由: {res.get('routing_action')}")
        return

    if args.resume:
        # 从检查点读取此前已确认的 user_spec 作为基底，仅用显式传入的
        # 参数覆盖对应字段，避免 CLI 缺省值把真人确认的配置静默降级。
        prior_spec = {}
        try:
            snapshot = get_graph(db_path).get_state(
                {"configurable": {"thread_id": args.thread_id}})
            prior = (snapshot.values or {}).get("user_spec") or {}
            if isinstance(prior, dict):
                prior_spec = dict(prior)
        except Exception:
            prior_spec = {}

        spec = dict(prior_spec)
        if args.title:
            spec["main_title"] = args.title
        if args.profile:
            spec["delivery_profile"] = args.profile
        if args.format:
            spec["output_format"] = args.format
        if args.lesson_minutes is not None:
            spec["lesson_minutes"] = args.lesson_minutes
        spec.setdefault("main_title", "学案")
        spec.setdefault("delivery_profile", "self_study")
        spec.setdefault("output_format", "both")

        token = os.environ.get("QCHAIN_PHASE0_TOKEN") or args.token

        print(f"[*] 恢复流水线: thread={args.thread_id}, "
              f"format={spec['output_format']}, profile={spec['delivery_profile']}")
        res = resume_pipeline(
            user_response={"user_spec": spec, "token": token},
            thread_id=args.thread_id,
            db_path=db_path,
            expected_self_dir=self_dir
        )
        print(f"[*] 执行阶段: {res.get('stage')}, 路由: {res.get('routing_action')}")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
