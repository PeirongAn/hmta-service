"""Structured file-based logging for HMTA Service.

每次启动创建 logs/{timestamp}/ 目录，内含按关注点分文件的日志：

    logs/
    └── 2026-03-16_20-05-33/
        ├── main.log          — 全量日志（所有级别）
        ├── llm_io.log        — LLM 请求/响应完整内容（task_planner + bt_builder）
        ├── pipeline.log      — 生成管线每步进度（task_planner/allocator/bt_builder/validator/fsm_bb_init）
        ├── allocation.log    — 量化分配详情（每个子任务评分、分配结果、注意力预算）
        ├── validation.log    — 约束校验详情（违规列表）
        ├── execution.log     — BT 执行 tick、FSM 状态变化
        └── zenoh.log         — Zenoh 发布/订阅事件

用法（在 main.py 中调用，替换 logging.basicConfig）::

    from app.log_setup import setup_logging
    run_dir = setup_logging(log_level="INFO")
    logger.info("日志目录: %s", run_dir)
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path


# ── 各关注点对应的 logger 名称前缀 ──────────────────────────────────────────────

_CHANNEL_MAP: dict[str, str] = {
    # logger name prefix → 目标文件名（不含.log）
    "app.generation.graph.task_planner":    "pipeline",
    "app.generation.graph.bt_builder":      "pipeline",
    "app.generation.graph.constraint_validator": "pipeline",
    "app.generation.graph.fsm_bb_init":     "pipeline",
    "app.capability.allocator":             "pipeline",
    "app.generation.graph.progress":        "pipeline",
    "app.execution.engine":                 "execution",
    "app.execution.tree_loader":            "execution",
    "app.execution.fsm":                    "execution",
    "app.execution.behaviours":             "execution",
    "app.execution.trace":                  "execution",
    "app.zenoh_bridge":                     "zenoh",
    "httpx":                                "zenoh",    # HTTP 请求（LLM API 走 httpx）
}

_LLM_IO_MARKER = "LLM request"   # task_planner / bt_builder 会在消息中包含此标记


# ── Filter: 把带特定标记的日志路由到 llm_io.log ────────────────────────────────

class _LLMIOFilter(logging.Filter):
    """允许 LLM 请求/响应日志通过（消息含 'LLM request' 或 'LLM response'）。"""
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return "LLM request" in msg or "LLM response" in msg or (
            # 缩进行（请求/响应的内容行以 "  " 开头且紧跟在 LLM 块之后）
            # 用 extra 字段标记
            getattr(record, "llm_io", False)
        )


class _LLMIOContextFilter(logging.Filter):
    """把 task_planner / bt_builder 里的缩进内容行也导入 llm_io.log。"""

    def __init__(self):
        super().__init__()
        self._in_llm_block: dict[str, bool] = {}  # logger_name → bool

    def filter(self, record: logging.LogRecord) -> bool:
        name = record.name
        msg = record.getMessage()
        if "LLM request" in msg or "LLM response" in msg:
            self._in_llm_block[name] = True
            return True
        if self._in_llm_block.get(name) and (
            msg.lstrip().startswith("[") or "──" in msg
        ):
            # 遇到下一个普通的 [task_id] 开头行但不是内容行则退出块
            # 内容行: 以 "  " 开头或包含 "model:" / "entities" / "task_plan" 等
            return False
        if self._in_llm_block.get(name):
            # 判断是否仍在 LLM 块内：如果是 INFO 且消息中有 task_id 前缀 "[bt_"
            # 但不是 "LLM" 类型 → 退出块
            stripped = msg.strip()
            if stripped.startswith("[bt_") or stripped.startswith("[unknown"):
                self._in_llm_block[name] = False
                return False
            return True
        return False


class _ChannelFilter(logging.Filter):
    """只允许属于指定 channel 的 logger 记录通过。"""

    def __init__(self, channel: str, channel_map: dict[str, str]):
        super().__init__()
        self._channel = channel
        self._channel_map = channel_map

    def filter(self, record: logging.LogRecord) -> bool:
        name = record.name
        for prefix, ch in self._channel_map.items():
            if name == prefix or name.startswith(prefix + "."):
                return ch == self._channel
        return False


class _AllocationFilter(logging.Filter):
    """把 allocator 的详细分配日志路由到 allocation.log。"""
    def filter(self, record: logging.LogRecord) -> bool:
        return record.name.startswith("app.capability.allocator")


class _ValidationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.name.startswith("app.generation.graph.constraint_validator")


class _ExecutionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return (
            record.name.startswith("app.execution.")
            or record.name.startswith("app.execution.fsm")
        )


class _ZenohFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.name.startswith("app.zenoh_bridge") or record.name == "httpx"


# ── 核心安装函数 ────────────────────────────────────────────────────────────────

def setup_logging(log_level: str = "INFO", logs_root: str | Path = "logs") -> Path:
    """配置分层文件日志，返回本次运行的日志目录路径。

    调用一次，在 main.py 的 ``logging.basicConfig`` 之前调用。
    """
    root_level = getattr(logging, log_level.upper(), logging.INFO)
    logs_root = Path(logs_root)

    # 创建本次运行目录
    run_dir = logs_root / datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    run_dir.mkdir(parents=True, exist_ok=True)

    fmt_full  = logging.Formatter("%(asctime)s [%(levelname)-8s] %(name)s: %(message)s")
    fmt_brief = logging.Formatter("%(asctime)s %(message)s")

    def _file_handler(filename: str, fmt: logging.Formatter) -> logging.FileHandler:
        h = logging.FileHandler(run_dir / filename, encoding="utf-8")
        h.setFormatter(fmt)
        h.setLevel(logging.DEBUG)
        return h

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)   # 根 logger 全量收集，各 handler 自行过滤

    # ── stdout：控制台只打摘要行，LLM 请求/响应内容行不输出（避免刷屏）──────
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt_full)
    console.setLevel(root_level)
    console.addFilter(_ConsoleFilter())
    root_logger.addHandler(console)

    # ── main.log：全量（DEBUG+） ───────────────────────────────────────────────
    h_main = _file_handler("main.log", fmt_full)
    root_logger.addHandler(h_main)

    # ── llm_io.log：LLM 请求/响应完整内容 ────────────────────────────────────
    h_llm = _file_handler("llm_io.log", fmt_full)
    h_llm.addFilter(_LLMContextBlock())
    root_logger.addHandler(h_llm)

    # ── pipeline.log：生成管线各步骤进度 ─────────────────────────────────────
    h_pipe = _file_handler("pipeline.log", fmt_brief)
    h_pipe.addFilter(_PipelineFilter())
    root_logger.addHandler(h_pipe)

    # ── allocation.log：分配详情 ──────────────────────────────────────────────
    h_alloc = _file_handler("allocation.log", fmt_full)
    h_alloc.addFilter(_AllocationFilter())
    root_logger.addHandler(h_alloc)

    # ── validation.log：约束校验 ──────────────────────────────────────────────
    h_valid = _file_handler("validation.log", fmt_full)
    h_valid.addFilter(_ValidationFilter())
    root_logger.addHandler(h_valid)

    # ── execution.log：BT 执行 ────────────────────────────────────────────────
    h_exec = _file_handler("execution.log", fmt_full)
    h_exec.addFilter(_ExecutionFilter())
    root_logger.addHandler(h_exec)

    # ── zenoh.log：Zenoh 收发 ─────────────────────────────────────────────────
    h_zenoh = _file_handler("zenoh.log", fmt_full)
    h_zenoh.addFilter(_ZenohFilter())
    root_logger.addHandler(h_zenoh)

    # 记录日志目录路径（写入 main.log）
    logging.getLogger("hmta.log_setup").info(
        "Run log directory: %s", run_dir.resolve()
    )
    return run_dir


# ── 复合 Filter：把 LLM 请求/响应的多行内容收集到 llm_io.log ─────────────────

class _LLMContextBlock(logging.Filter):
    """跟踪 '──' 分隔线块，把整块 LLM 请求/响应内容导入 llm_io.log。"""

    _LLM_LOGGERS = {
        "app.generation.graph.task_planner",
        "app.generation.graph.bt_builder",
    }

    def __init__(self):
        super().__init__()
        self._active: set[str] = set()   # 正在记录块的 logger name

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name not in self._LLM_LOGGERS:
            return False
        msg = record.getMessage()
        # 进入块：含 "LLM request" 或 "LLM response" 的行
        if "LLM request" in msg or "LLM response" in msg:
            self._active.add(record.name)
            return True
        # 在块内：以 [task_id]   (缩进) 开头的行
        if record.name in self._active:
            # 退出条件：日志来自同一 logger 但是 "started/completed" 等流程行
            stripped = msg.strip()
            if any(kw in stripped for kw in ("started", "completed", "repair=")):
                self._active.discard(record.name)
                return False
            return True
        return False


class _PipelineFilter(logging.Filter):
    """只收集生成管线的进度行（started / completed / PASSED / FAILED / done）。"""

    _PIPELINE_LOGGERS = {
        "app.generation.graph.task_planner",
        "app.generation.graph.bt_builder",
        "app.generation.graph.constraint_validator",
        "app.generation.graph.fsm_bb_init",
        "app.capability.allocator",
        "app.execution.tree_loader",
    }
    _KEYWORDS = ("started", "completed", "result=", "done —", "BT loaded",
                 "attention budget", "upgraded to", "no robot candidates",
                 "no task_plan")

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name not in self._PIPELINE_LOGGERS:
            return False
        msg = record.getMessage()
        return any(kw in msg for kw in self._KEYWORDS)


class _ConsoleFilter(logging.Filter):
    """控制台过滤：屏蔽 LLM 请求/响应的内容行（多行大文本），只保留摘要行。

    内容行特征：来自 task_planner / bt_builder，消息以若干空格缩进（"  " 开头）
    或者是 "──" 分隔线后的纯 JSON / Markdown 内容。
    摘要行（started/completed/LLM request .../LLM response ...）正常输出。
    """

    _LLM_LOGGERS = {
        "app.generation.graph.task_planner",
        "app.generation.graph.bt_builder",
    }

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name not in self._LLM_LOGGERS:
            return True  # 非 LLM 节点：全部输出到控制台
        msg = record.getMessage()
        # 摘要行保留：含 "LLM request/response"、"started"、"completed"、task_id 格式
        if any(kw in msg for kw in ("LLM request", "LLM response", "started", "completed", "repair=")):
            return True
        # 内容行（缩进的 JSON / 文本）屏蔽
        stripped = msg.lstrip()
        if stripped != msg:   # 有缩进 → 内容行
            return False
        return True
