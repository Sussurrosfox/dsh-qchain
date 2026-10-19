"""gate_runner.py — 进程内批量门禁执行器（audit §12.6 / 任务7）。

动机：ScriptRunner.run_script 每个门禁脚本新起一个子进程（script_runner.py:41-72），
T3 五连门禁（pedagogy/numbering/dialogue/fidelity/budget）要付 5 份进程固定成本。
GateRunner 把「纯 Python 检查」合并为一次进程内批量执行：

- 每个脚本只 import 一次（importlib 按文件位置装载，注册 sys.modules，
  支持脚本间的 flat import，如 `import evidence`）；
- 复用 ScriptRunner 的 _quarantine_report / _synthesize_gate_report，
  报告文件名、.stale 隔离、新鲜度判定与 PASS/REVIEW/REJECT/ERROR 语义
  与子进程路径逐字一致（不改变任何调用方的判读逻辑）；
- 一次批量共享一份输入快照（evidence.collect_inputs + content_digest），
  以 batch_input_digest 附在每份报告上，供 provenance 对账。

边界（红线）：
- 只合并纯 Python 检查；XeLaTeX 编译 / HTML 渲染 / 外部构建仍走子进程
  （进程内跑编译器没有隔离，崩溃会拖死整个流水线进程）；
- 本执行器**不加缓存**：过期报告由 .stale 隔离 + 新鲜度判定自然失效，
  不存在「旧 PASS 冒充新结果」的通道；
- 进程内装载失败或执行抛错时，逐 gate 收敛为 REJECT/ERROR GateReport，
  绝不向上抛异常打断图执行（与子进程失败的语义对齐）。
"""

import contextlib
import importlib.util
import io
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from .script_runner import ScriptRunner, _iso
from ..state import GateReport, GateIssue

#: 可进程内合并执行的纯 Python 门禁脚本（gate 名 → 脚本文件名）。
#: XeLaTeX/HTML/外部构建不在此列；新增门禁脚本只需遵循
#: `def main()` + argparse(sys.argv) + `--json PATH` 报告协议即可入表。
PURE_PYTHON_GATES: Dict[str, str] = {
    "check_dialogue": "check_dialogue.py",
    "check_numbering": "check_numbering.py",
    "check_budget": "check_budget.py",
    "check_pedagogy": "check_pedagogy.py",
    "check_fidelity": "check_fidelity.py",
    "check_semantic": "check_semantic.py",
    "check_dispatch": "check_dispatch.py",
    "check_scope": "check_scope.py",
    "check_provenance": "check_provenance.py",
}

#: 进程内执行的软护栏：单批量门禁数量上限（防止误把全部门禁塞进一批）。
MAX_BATCH_SIZE = 16


class GateRunner:
    """进程内批量门禁执行器（组合 ScriptRunner 复用报告语义）。"""

    def __init__(self, project_root: str, scripts_dir: Optional[str] = None):
        self.project_root = os.path.abspath(project_root)
        self._runner = ScriptRunner(self.project_root, scripts_dir)
        self.scripts_dir = self._runner.scripts_dir
        self._modules: Dict[str, Any] = {}
        self._path_ready = False

    # ------------------------------------------------------------------
    # 模块装载（进程内 import，一次一批）
    # ------------------------------------------------------------------
    def _ensure_scripts_on_path(self) -> None:
        if self._path_ready:
            return
        if self.scripts_dir not in sys.path:
            sys.path.insert(0, self.scripts_dir)
        self._path_ready = True

    def _import_module(self, script_name: str) -> Any:
        """按文件位置装载门禁脚本模块（幂等，缓存复用）。"""
        if script_name in self._modules:
            return self._modules[script_name]
        self._ensure_scripts_on_path()
        script_path = os.path.join(self.scripts_dir, script_name)
        if not os.path.isfile(script_path):
            raise FileNotFoundError(f"Script not found: {script_path}")
        stem = os.path.splitext(os.path.basename(script_name))[0]
        spec = importlib.util.spec_from_file_location(stem, script_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot build import spec for {script_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[stem] = module  # 支持 dataclasses/类型注解等自引用解析
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(stem, None)
            raise
        self._modules[script_name] = module
        return module

    # ------------------------------------------------------------------
    # 进程内执行
    # ------------------------------------------------------------------
    def _run_in_process(self, module: Any,
                        args: List[str]) -> Tuple[int, str, str]:
        """补丁 sys.argv 后调用 module.main()，捕获其退出码与输出。"""
        old_argv = sys.argv
        out_buf: io.StringIO = io.StringIO()
        err_buf: io.StringIO = io.StringIO()
        try:
            sys.argv = [getattr(module, "__file__", None) or "gate_runner"] \
                + [str(a) for a in args]
            with contextlib.redirect_stdout(out_buf), \
                    contextlib.redirect_stderr(err_buf):
                code = module.main()
            return int(code or 0), out_buf.getvalue(), err_buf.getvalue()
        except SystemExit as exc:  # 有的脚本可能 sys.exit(main())
            code = exc.code
            if code is None:
                code = 0
            elif not isinstance(code, int):
                code = 1
            return code, out_buf.getvalue(), err_buf.getvalue()
        finally:
            sys.argv = old_argv

    # ------------------------------------------------------------------
    # 共享输入快照
    # ------------------------------------------------------------------
    def batch_input_digest(self) -> Optional[str]:
        """一次批量共享的输入内容摘要（manifest digest）。

        取不到（evidence 不可导入等）时返回 None——只作 provenance，
        不影响门禁判定。
        """
        try:
            evidence = self._import_module("evidence.py")
            manifest = evidence.collect_inputs(self.project_root)
            return evidence.content_digest(manifest)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # 单门禁 / 批量化执行
    # ------------------------------------------------------------------
    def run_gate(
        self,
        gate_name: str,
        script_name: str,
        args: List[str],
        report_rel_path: str,
        timeout: int = 300,
        run_id: Optional[str] = None,
        force_subprocess: bool = False
    ) -> GateReport:
        """执行单个门禁；语义与 ScriptRunner.run_gate_script 一致。

        force_subprocess=True 或进程内装载失败时回退子进程执行
        （回退不改变门禁语义，仅在报告上标记 execution_mode）。
        """
        full_report_path = os.path.join(
            self.project_root, report_rel_path.replace("/", os.sep))
        report_dir = os.path.dirname(full_report_path)
        if report_dir:
            os.makedirs(report_dir, exist_ok=True)

        if force_subprocess:
            report = self._runner.run_gate_script(
                gate_name, script_name, args, report_rel_path,
                timeout=timeout, run_id=run_id)
            report["execution_mode"] = "subprocess"
            return report

        try:
            module = self._import_module(script_name)
        except Exception as exc:
            report = self._runner.run_gate_script(
                gate_name, script_name, args, report_rel_path,
                timeout=timeout, run_id=run_id)
            report["execution_mode"] = "subprocess-fallback"
            report["fallback_reason"] = f"in-process import failed: {exc}"
            return report

        self._runner._quarantine_report(full_report_path, run_id=run_id)
        t_before = time.time()
        cmd_args = list(args)
        if "--json" not in cmd_args and not any(
                a.startswith("--json=") for a in cmd_args):
            cmd_args.extend(["--json", full_report_path])

        try:
            code, stdout, stderr = self._run_in_process(module, cmd_args)
        except Exception as exc:  # 进程内崩 → 与子进程执行失败同语义（code=1）
            code, stdout, stderr = 1, "", f"in-process execution failed: {exc}"

        report = self._runner._synthesize_gate_report(
            gate_name, code, stdout, stderr, full_report_path, t_before, run_id)
        report["execution_mode"] = "in-process"
        return report

    def run_gates(
        self,
        specs: List[Dict[str, Any]],
        run_id: Optional[str] = None
    ) -> Dict[str, GateReport]:
        """批量执行门禁，返回 {gate_name: GateReport}。

        specs 每项：{"gate_name", "script_name", "args", "report_rel_path",
                     可选 "timeout" / "force_subprocess"}。
        一批共享一份输入快照；单个 gate 失败不影响其余 gate 执行。
        """
        if len(specs) > MAX_BATCH_SIZE:
            raise ValueError(
                f"gate batch too large: {len(specs)} > {MAX_BATCH_SIZE}")
        digest = self.batch_input_digest()
        reports: Dict[str, GateReport] = {}
        for spec in specs:
            gate_name = str(spec["gate_name"])
            try:
                report = self.run_gate(
                    gate_name=gate_name,
                    script_name=str(spec["script_name"]),
                    args=list(spec.get("args") or []),
                    report_rel_path=str(spec["report_rel_path"]),
                    timeout=int(spec.get("timeout") or 300),
                    run_id=spec.get("run_id", run_id),
                    force_subprocess=bool(spec.get("force_subprocess", False)),
                )
            except Exception as exc:  # 双保险：绝不向上抛
                report = GateReport(
                    gate_name=gate_name,
                    status="ERROR",
                    passed=False,
                    issues=[{
                        "category": "EXECUTION_FAILURE",
                        "severity": "HIGH_RISK",
                        "message": f"gate runner failed: {exc}",
                    }],
                    exit_code=1,
                    report_path=None,
                    report_is_fresh=False,
                    run_id=run_id,
                )
                report["execution_mode"] = "in-process"
            report["batch_input_digest"] = digest
            reports[gate_name] = report
        return reports
