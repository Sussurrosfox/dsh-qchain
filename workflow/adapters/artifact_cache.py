"""artifact_cache.py — 派生物缓存（audit §17 任务5 / §12.3）

问题：每次微调/重跑都重跑完整 T1（三张映射表）与 T2（全部切片），即使输入
（来源清单、目标合同、配置、提示词）毫无变化。本模块按内容键缓存 Agent
产物，使「相同输入跑两次，第二次 worker submit 为 0」。

缓存键：sha256(stage + scope + input_digest + config_digest + prompt_version)
    stage         请求阶段（T1_MAPPING / T2_<SEC> …）
    scope         小节或映射单元
    input_digest  派发单输入摘要（build_request 确定性计算，覆盖
                 instruction/context/artifacts/prompt_file 变化）
    config_digest config.yaml 摘要（档位/课时/模式/内容策略等配置变化）
    prompt_version 提示词模板内容摘要（提示词改版即失效）

命中条件（先校验所有 digest 再返回，audit 红线：不以文件存在/mtime 代替
digest 校验）：
    1. 条目存在且键一致；
    2. 缓存目录内全部产物文件在位且 sha256 与记录一致；
    3. 产物相对路径全部位于 self_dir 内（防路径逃逸）。
任一不满足 → miss（删除失效条目，走正常派发路径重跑）。
"""

import hashlib
import json
import os
import shutil
from typing import Any, Dict, List, Optional

CACHE_DIR_REL = ".pd/artifact-cache"
CACHE_VERSION = 1
_EMPTY_SHA = hashlib.sha256(b"").hexdigest()


def file_digest(path: str) -> Optional[str]:
    """文件 sha256；缺失/不可读返回 None。"""
    try:
        if not os.path.isfile(path):
            return None
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def config_digest(self_dir: str) -> str:
    """self_dir/config.yaml 内容摘要（缺失 → 空串摘要，确定不变）。"""
    return file_digest(os.path.join(self_dir or ".", "config.yaml")) or _EMPTY_SHA


def prompt_version(self_dir: str, prompt_file: str) -> str:
    """提示词模板内容摘要（缺失 → 空串摘要）。

    解析顺序：插件自带 prompts/（agent_nodes 中 prompt_file 的语义参照点，
    提示词改版即使输入摘要不变也使缓存失效）→ 工程 self/prompts/ → 工程
    上级 prompts/（允许工程级覆盖，极少使用）。
    """
    rel = str(prompt_file or "")
    if not rel:
        return _EMPTY_SHA
    rel = rel.replace("\\", "/")
    if rel.startswith("prompts/"):
        rel = rel[len("prompts/"):]
    plugin_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    candidates = [
        os.path.join(plugin_root, "prompts", rel),
        os.path.join(self_dir or ".", "prompts", rel),
        os.path.join(self_dir or ".", rel),
        os.path.join(os.path.abspath(os.path.join(self_dir or ".", "..")),
                     "prompts", rel),
    ]
    for path in candidates:
        digest = file_digest(path)
        if digest:
            return digest
    return _EMPTY_SHA


def cache_key(*, stage: str, scope: str, input_digest: str,
              config_digest: str, prompt_version: str) -> str:
    """确定性缓存键（audit §17 任务5 公式）。"""
    payload = "|".join([
        "v%d" % CACHE_VERSION, str(stage), str(scope),
        str(input_digest), str(config_digest), str(prompt_version),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ArtifactCache:
    """self_dir 级派生物缓存（目录 .pd/artifact-cache/<key>/）。"""

    def __init__(self, self_dir: str):
        self.self_dir = self_dir or "."
        self.root = os.path.join(self.self_dir, *CACHE_DIR_REL.split("/"))

    # -- 内部工具 -------------------------------------------------------

    def _entry_path(self, key: str) -> str:
        return os.path.join(self.root, key, "entry.json")

    def _safe_target(self, rel: str) -> Optional[str]:
        """把 artifacts 相对路径解析到 self_dir 内（防逃逸）。"""
        rel = str(rel).replace("\\", "/").lstrip("/")
        if not rel or rel.startswith("..") or ".." in rel.split("/"):
            return None
        target = os.path.join(self.self_dir, *rel.split("/"))
        base = os.path.abspath(self.self_dir)
        if os.path.commonpath([os.path.abspath(target), base]) != base:
            return None
        return target

    # -- 查询 / 命中 ----------------------------------------------------

    def lookup(self, *, stage: str, scope: str, input_digest: str,
               config_digest: str, prompt_version: str,
               artifacts: Optional[List[str]] = None
               ) -> Optional[Dict[str, Any]]:
        """按键查询并校验全部 digest；命中返回条目（含 restored 标记），
        未命中/失效返回 None。"""
        key = cache_key(stage=stage, scope=scope, input_digest=input_digest,
                        config_digest=config_digest,
                        prompt_version=prompt_version)
        entry_path = self._entry_path(key)
        if not os.path.isfile(entry_path):
            return None
        try:
            with open(entry_path, "r", encoding="utf-8-sig") as f:
                entry = json.load(f)
        except (OSError, ValueError):
            self._drop(key)
            return None
        if not isinstance(entry, dict) or entry.get("key") != key:
            self._drop(key)
            return None
        arts = entry.get("artifacts")
        if not isinstance(arts, dict) or not arts:
            self._drop(key)
            return None
        if artifacts is not None and sorted(arts.keys()) != sorted(artifacts):
            # 产物清单与本次请求不一致 → 不是本 scope 的缓存
            return None
        # 先校验所有 digest 再返回：缓存副本必须与记录逐一吻合
        cache_dir = os.path.join(self.root, key)
        for rel, expected in arts.items():
            if self._safe_target(rel) is None:
                self._drop(key)
                return None
            cached = os.path.join(cache_dir, *str(rel).split("/"))
            actual = file_digest(cached)
            if actual is None or actual != expected:
                self._drop(key)
                return None
        entry["restored"] = False
        return entry

    def restore(self, entry: Dict[str, Any]) -> bool:
        """把命中条目中的产物副本写回 self_dir（原子替换）。"""
        key = entry.get("key")
        if not key:
            return False
        cache_dir = os.path.join(self.root, key)
        for rel in entry.get("artifacts", {}):
            target = self._safe_target(rel)
            if target is None:
                return False
            src = os.path.join(cache_dir, *str(rel).split("/"))
            if not os.path.isfile(src):
                return False
            os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
            tmp = target + ".cache-tmp"
            shutil.copyfile(src, tmp)
            os.replace(tmp, target)
        entry["restored"] = True
        return True

    def _drop(self, key: str) -> None:
        shutil.rmtree(os.path.join(self.root, key), ignore_errors=True)

    # -- 写入 ----------------------------------------------------------

    def store(self, *, stage: str, scope: str, input_digest: str,
              prompt_version: str, artifacts: List[str],
              config_digest: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """把 self_dir 中的产物副本存入缓存并返回条目。

        任一产物缺失/越界 → 不缓存（返回 None），绝不写半成品条目。
        """
        cfg_digest = config_digest or _EMPTY_SHA
        key = cache_key(stage=stage, scope=scope, input_digest=input_digest,
                        config_digest=cfg_digest, prompt_version=prompt_version)
        cache_dir = os.path.join(self.root, key)
        digests: Dict[str, str] = {}
        for rel in artifacts:
            target = self._safe_target(rel)
            if target is None:
                return None
            digest = file_digest(target)
            if digest is None:
                return None
            digests[str(rel).replace("\\", "/")] = digest
        if not digests:
            return None
        os.makedirs(cache_dir, exist_ok=True)
        for rel in digests:
            target = self._safe_target(rel)
            src = os.path.join(cache_dir, *rel.split("/"))
            os.makedirs(os.path.dirname(src) or ".", exist_ok=True)
            shutil.copyfile(target, src)
        entry = {
            "schema_version": CACHE_VERSION,
            "key": key,
            "stage": str(stage),
            "scope": str(scope),
            "input_digest": str(input_digest),
            "config_digest": cfg_digest,
            "prompt_version": str(prompt_version),
            "artifacts": digests,
        }
        with open(self._entry_path(key), "w", encoding="utf-8") as f:
            json.dump(entry, f, ensure_ascii=False, indent=2)
        return entry

    # -- 失效 ----------------------------------------------------------

    def invalidate(self, scope: Optional[str] = None) -> int:
        """scope 级失效（audit §12.3）；scope=None 时清空全部缓存。

        返回删除的条目数。来源/目标/前置依赖/规划规则变化时由调用方触发。
        """
        if not os.path.isdir(self.root):
            return 0
        removed = 0
        for name in os.listdir(self.root):
            entry_path = os.path.join(self.root, name, "entry.json")
            if scope is not None:
                try:
                    with open(entry_path, "r", encoding="utf-8-sig") as f:
                        entry = json.load(f)
                except (OSError, ValueError):
                    continue
                if entry.get("scope") != scope:
                    continue
            self._drop(name)
            removed += 1
        return removed
