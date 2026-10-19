"""script_runner.py — Safe executor for underlying verification and build scripts.
Encapsulates script invocations, pathing, JSON report parsing, and GateReport synthesis.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from typing import Dict, Any, List, Optional, Tuple
from ..state import GateReport, GateIssue

PASS_STATUSES = ("pass", "PASS", "ok", "OK")
STALE_DIRNAME = ".stale"


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


class ScriptRunner:
    def __init__(self, project_root: str, scripts_dir: Optional[str] = None):
        self.project_root = os.path.abspath(project_root)
        if scripts_dir:
            self.scripts_dir = os.path.abspath(scripts_dir)
        else:
            # Default to ../../scripts relative to this file
            current_dir = os.path.dirname(os.path.abspath(__file__))
            self.scripts_dir = os.path.abspath(os.path.join(current_dir, "..", "..", "scripts"))

    def _get_env(self) -> Dict[str, str]:
        env = os.environ.copy()
        pythonpath = env.get("PYTHONPATH", "")
        extra_paths = [self.scripts_dir]
        if pythonpath:
            extra_paths.append(pythonpath)
        env["PYTHONPATH"] = os.pathsep.join(extra_paths)
        return env

    def run_script(
        self,
        script_name: str,
        args: List[str],
        timeout: int = 300,
        cwd: Optional[str] = None
    ) -> Tuple[int, str, str]:
        """Run a Python script in self.scripts_dir."""
        script_path = os.path.join(self.scripts_dir, script_name)
        if not os.path.exists(script_path):
            raise FileNotFoundError(f"Script not found: {script_path}")

        cmd = [sys.executable, script_path] + args
        work_dir = cwd or self.project_root

        try:
            proc = subprocess.run(
                cmd,
                cwd=work_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=self._get_env(),
                timeout=timeout
            )
            return proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            return 124, "", f"Timeout expired after {timeout} seconds: {exc}"
        except Exception as exc:
            return 1, "", f"Execution failed: {exc}"

    def _quarantine_report(self, full_report_path: str,
                           run_id: Optional[str] = None) -> Optional[str]:
        """Archive existing stale report before running a new check.

        run_id 提供时归档到 .stale/<run_id>/ 不可变子目录（audit §12.6：
        按 run 归档，避免不同轮次的旧报告互相覆盖）；缺省保持旧布局。
        """
        if not os.path.isfile(full_report_path):
            return None
        stale_dir = os.path.join(os.path.dirname(full_report_path), STALE_DIRNAME)
        if run_id:
            stale_dir = os.path.join(stale_dir, str(run_id))
        try:
            os.makedirs(stale_dir, exist_ok=True)
            stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime())
            base = os.path.basename(full_report_path)
            archived = os.path.join(stale_dir, f"{base}.{stamp}")
            n = 1
            while os.path.exists(archived):
                n += 1
                archived = os.path.join(stale_dir, f"{base}.{stamp}.{n}")
            shutil.move(full_report_path, archived)
            return archived
        except OSError:
            try:
                os.remove(full_report_path)
            except OSError:
                pass
            return None

    def _read_report(self, full_report_path: str) -> Tuple[Dict[str, Any], Optional[str]]:
        """Read and parse JSON report file."""
        try:
            with open(full_report_path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {}, f"Report is not a JSON object: {full_report_path}"
            return data, None
        except Exception as e:
            return {}, f"Failed to parse report {full_report_path}: {e}"

    def run_gate_script(
        self,
        gate_name: str,
        script_name: str,
        args: List[str],
        report_rel_path: str,
        timeout: int = 300,
        run_id: Optional[str] = None
    ) -> GateReport:
        """Run verification script with JSON report output and construct GateReport."""
        full_report_path = os.path.join(self.project_root, report_rel_path.replace("/", os.sep))
        report_dir = os.path.dirname(full_report_path)
        if report_dir:
            os.makedirs(report_dir, exist_ok=True)

        self._quarantine_report(full_report_path, run_id=run_id)
        t_before = time.time()

        # Inject --json argument
        cmd_args = list(args)
        if "--json" not in cmd_args and not any(a.startswith("--json=") for a in cmd_args):
            cmd_args.extend(["--json", full_report_path])

        code, stdout, stderr = self.run_script(script_name, cmd_args, timeout=timeout)
        return self._synthesize_gate_report(
            gate_name, code, stdout, stderr, full_report_path, t_before, run_id
        )

    def _synthesize_gate_report(
        self,
        gate_name: str,
        code: int,
        stdout: str,
        stderr: str,
        full_report_path: str,
        t_before: float,
        run_id: Optional[str] = None
    ) -> GateReport:
        """把一次门禁执行（子进程或进程内）收敛为 GateReport。

        GateRunner 进程内批量执行复用本方法，保证两种执行路径的
        隔离/新鲜度/状态合成语义逐字一致（audit §12.6）。
        """
        report_data: Dict[str, Any] = {}
        report_mtime: Optional[float] = None
        is_fresh = False
        read_err: Optional[str] = None

        if os.path.isfile(full_report_path):
            try:
                report_mtime = os.path.getmtime(full_report_path)
                is_fresh = (report_mtime >= t_before - 1.0)
            except OSError:
                is_fresh = False
            report_data, read_err = self._read_report(full_report_path)

        issues: List[GateIssue] = []
        raw_issues = report_data.get("issues", [])
        if isinstance(raw_issues, list):
            for it in raw_issues:
                if isinstance(it, dict):
                    issues.append({
                        "category": str(it.get("category", "UNKNOWN")),
                        "severity": it.get("severity", "WARN"),
                        "message": str(it.get("message", "")),
                        "hash": it.get("hash"),
                        "file": it.get("file"),
                        "line": it.get("line")
                    })
                elif isinstance(it, str):
                    issues.append({
                        "category": "GENERIC",
                        "severity": "WARN",
                        "message": it
                    })

        status_field = str(report_data.get("status", "")).strip().lower()

        # Synthesis logic: script exit code 0 AND report status pass AND fresh
        if code != 0:
            status = "ERROR" if code >= 120 else "REJECT"
            passed = False
            err_msg = stderr.strip() or stdout.strip() or f"Script exited with {code}"
            issues.append({
                "category": "EXECUTION_FAILURE",
                "severity": "HIGH_RISK",
                "message": err_msg
            })
        elif not is_fresh or not report_data:
            status = "ERROR"
            passed = False
            issues.append({
                "category": "REPORT_MISSING_OR_STALE",
                "severity": "HIGH_RISK",
                "message": read_err or "Report not generated or stale"
            })
        elif status_field in ("pass", "ok"):
            status = "PASS"
            passed = True
        elif status_field in ("review", "pending"):
            status = "REVIEW"
            passed = False
        else:
            status = "REJECT"
            passed = False

        return GateReport(
            gate_name=gate_name,
            status=status,
            passed=passed,
            issues=issues,
            raw_report=report_data,
            exit_code=code,
            report_path=full_report_path,
            report_generated_at=_iso(report_mtime) if report_mtime else None,
            report_is_fresh=is_fresh,
            run_id=run_id or report_data.get("run_id")
        )
