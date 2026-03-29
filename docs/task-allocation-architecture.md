# HMTA Service — 任务分配架构全景文档

> **最后更新**: 2026-03-20
> **涵盖范围**: 任务分解、超图构建、能力注册与推理、任务分配、行为树生成、约束验证、实验基础设施、动态重分配

---

## 目录

1. [整体架构](#1-整体架构)
2. [端到端流程](#2-端到端流程)
3. [任务分解 (Task Planner)](#3-任务分解-task-planner)
4. [超图结构与构建 (HyperGraph)](#4-超图结构与构建-hypergraph)
5. [能力注册 (Capability Registry)](#5-能力注册-capability-registry)
6. [有效能力推导 (Effective Capability Resolver)](#6-有效能力推导-effective-capability-resolver)
7. [任务分配 (Allocator)](#7-任务分配-allocator)
8. [行为树生成 (BT Builder)](#8-行为树生成-bt-builder)
9. [约束验证 (Constraint Validator)](#9-约束验证-constraint-validator)
10. [FSM / 黑板初始化](#10-fsm--黑板初始化)
11. [执行引擎 (Execution Engine)](#11-执行引擎-execution-engine)
12. [实验与学习基础设施](#12-实验与学习基础设施)
13. [动态重分配](#13-动态重分配)
14. [配置文件说明](#14-配置文件说明)
15. [Zenoh 消息拓扑](#15-zenoh-消息拓扑)
16. [相关文档](#相关文档)

---

## 1. 整体架构

```
┌─────────────────────────────────────────────────────────────────┐
│                         HMTA Service                            │
│                                                                 │
│  ┌──────────┐  ┌──────────────────────────────────────────────┐ │
│  │ FastAPI   │  │  LangGraph Generation Pipeline              │ │
│  │ /api/v1/* │  │                                              │ │
│  └────┬─────┘  │  Task     →  Allocator  →  BT       →  Val. │ │
│       │        │  Planner      (定量)       Builder     idator│ │
│       │        │  (LLM)                    (LLM)       (规则) │ │
│       │        │                                     ↙        │ │
│       │        │                           bt_builder ← FAILED│ │
│       │        │                                              │ │
│       │        │  PASSED → fsm_bb_init → 加载到执行引擎       │ │
│       │        └──────────────────────────────────────────────┘ │
│       │                                                         │
│  ┌────┴──────────────────┐  ┌─────────────────────────────────┐ │
│  │ Capability Subsystem  │  │  Execution Engine (py_trees)    │ │
│  │  Registry             │  │  Tick → CommandAction           │ │
│  │  EffectiveResolver    │  │       → CommandResolver         │ │
│  │  HyperGraph           │  │       → Zenoh 指令 → UE5 实体  │ │
│  │  Ontology             │  └─────────────────────────────────┘ │
│  └───────────────────────┘                                      │
│                                                                 │
│  ┌────────────────────────┐  ┌────────────────────────────────┐ │
│  │ Experiment Infra       │  │  ZenohBridge                   │ │
│  │  Store (SQLite)        │  │  订阅/发布 ← → IDE前端 + UE5  │ │
│  │  Learner (Phase A/B)   │  └────────────────────────────────┘ │
│  │  Controller            │                                     │
│  │  Collector             │                                     │
│  └────────────────────────┘                                     │
└─────────────────────────────────────────────────────────────────┘
```

**技术栈**:
- **生成管线**: LangGraph (有向图状态机) + LangChain (LLM 调用)
- **执行引擎**: py_trees (行为树 tick 循环)
- **通信**: Zenoh (低延迟 pub/sub)
- **学习框架**: scikit-learn (Ridge/GP) + scipy (优化)
- **存储**: SQLite (实验记录)

---

## 2. 端到端流程

### 2.1 生成流程

```
用户在 IDE 输入 "机器狗找炸弹"
  │
  ▼
IDE 前端 → WebSocket → zho-core → Zenoh: zho/bt/generate/request
  │
  ▼
HMTA main.py: _handle_generation_request()
  │
  ├── 1. build_initial_state(): 收集 entities, environment, task_context
  │
  ├── 2. LangGraph.ainvoke(state)
  │     │
  │     ├─ task_planner_node  → 任务分解 (LLM)
  │     ├─ allocator_node     → 任务分配 (定量计算)
  │     ├─ bt_builder_node    → 行为树生成 (LLM)
  │     ├─ validator_node     → 约束验证 (规则)
  │     │   ├─ FAILED → bt_builder_node (重试, 最多3次)
  │     │   └─ PASSED ↓
  │     └─ fsm_bb_init_node   → FSM/黑板初始化
  │
  ├── 3. engine.load(bt_json, blackboard_init, fsm_defs)
  │
  └── 4. Zenoh: zho/bt/generate/{task_id}/result → IDE
```

### 2.2 实体注册流程

```
UE5 实体上线
  │
  ▼
Zenoh: zho/entity/registry (机器人/人/设备)
  │
  ▼
main.py: _on_entity_registry()
  │
  ├─ 机器人 → CapabilityRegistry.register_entity()
  │           → HyperGraph: 实体节点 + 能力节点 + has_capability 边
  │
  ├─ 操作员 → _resolve_human_profile() (合并 human-profiles.yaml)
  │         → CapabilityRegistry.register_human_with_devices()
  │           → HyperGraph: 实体 + 设备 + 通道节点
  │           → EffectiveCapabilityResolver.resolve_and_apply()
  │           → HyperGraph: 推导出的人类能力边
  │
  └─ 设备状态更新 → CapabilityRegistry.update_device_status()
                   → 重新计算通道质量 → 刷新能力边权重
```

---

## 3. 任务分解 (Task Planner)

**文件**: `app/generation/graph/task_planner.py`
**类型**: LLM 节点
**Prompt**: `app/generation/prompts/task_planner.md`

### 功能

将用户的自然语言目标分解为结构化的 `TaskPlan`:

```yaml
task_plan:
  mission_name: "bomb_search"
  phases:
    - phase_id: "phase_1"
      name: "搜索阶段"
      subtasks: [...]
    - phase_id: "phase_2"
      name: "排爆阶段"
      subtasks: [...]
  subtasks:
    - id: "st_01"
      name: "区域A巡逻搜索"
      phase: "phase_1"
      required_capabilities: ["patrol", "detect"]
      priority: 1
      risk_level: "medium"
      estimated_duration_sec: 120
      dependencies: []
    - id: "st_02"
      name: "可疑物标记"
      required_capabilities: ["ar_annotate"]
      priority: 2
      dependencies: ["st_01"]
```

### 关键逻辑

1. **输入预处理**: `_strip_extensions()` 移除实体的设备扩展信息，避免 LLM 过度关注底层细节
2. **LLM 链**: `ChatOpenAI` → `StructuredOutput(TaskPlan)` → 输出标准化的 JSON
3. **输出**: `state["task_plan"]` — 包含 phases、subtasks、依赖关系、所需能力

---

## 4. 超图结构与构建 (HyperGraph)

**文件**: `app/capability/hypergraph.py`

### 4.1 节点类型 (HNode)

| kind | 说明 | 示例 |
|------|------|------|
| `entity` | 执行实体 | 机器狗1、操作员张三 |
| `capability` | 能力 | patrol、detect、approve |
| `task` | 子任务 | st_01: 区域A巡逻 |
| `device` | 穿戴设备 | ring_01、xr_01 |
| `channel` | 交互通道 | tap_command、spatial_view |

```python
HNode(id="dog1", kind="entity", attrs={
    "display_name": "机器狗1",
    "type": "robot_dog",
    "state": "IDLE",
    "battery": 0.85,
    "position": {"x": -372, "y": -1467, "z": 108}
})
```

### 4.2 边类型 (HEdge)

| kind | 连接 | 权重含义 |
|------|------|----------|
| `has_capability` | entity ↔ capability | 熟练度 (0~1) |
| `requires` | task ↔ capability | 重要性 (0~1) |
| `equips` | entity ↔ device | 1.0 (固定) |
| `provides` | device ↔ channel | 通道质量 (0~1, 受电量/状态影响) |
| `enables` | channels → capability | 1.0 (规则推导) |
| `collaborates` | entity ↔ entity | 协作权重 |

```python
HEdge(id="hc_dog1_patrol", kind="has_capability",
      nodes=frozenset({"dog1", "patrol"}),
      weight=0.9,
      attrs={"optimal_x": 0.12, "optimal_x_confidence": 0.75})
```

### 4.3 超图示意 (找炸弹场景)

```
                    ┌─────────┐
                    │ patrol  │ (capability)
                   ╱└────┬────┘╲
        has_cap   ╱      │      ╲ has_cap
        w=0.9    ╱       │       ╲ w=0.85
    ┌────────┐  ╱   ┌────┴────┐   ╲  ┌────────┐
    │  dog1  │──    │ detect  │    ──│  dog2  │
    │(entity)│╲     │(capab.) │    ╱ │(entity)│
    └────────┘ ╲    └─────────┘   ╱  └────────┘
                ╲        │       ╱
     has_cap     ╲       │requires
     w=0.9        ╲      │      ╱ has_cap w=0.8
                   ╲┌────┴────┐╱
                    │ st_01   │ (task: 巡逻搜索)
                    │ req=[   │
                    │ patrol, │
                    │ detect] │
                    └─────────┘

    ┌────────────┐  equips  ┌─────────┐ provides ┌──────────┐
    │operator-01 │─────────│  xr_01  │─────────│spatial_  │
    │  (entity)  │          │(device) │  w=1.0   │view(ch.) │
    └──────┬─────┘          └─────────┘          └─────┬────┘
           │ equips          ┌─────────┐               │
           ├────────────────│ ring_01 │               │enables
           │                 │(device) │               │
           │                 └────┬────┘          ┌────┴─────┐
           │                      │ provides      │ observe  │
           │                      ▼               │(capab.)  │
           │                ┌───────────┐         └──────────┘
           │                │tap_command│
           │                │ (channel) │──enables──▶ approve
           │                └───────────┘            (capability)
           │
           │ has_capability (推导)
           ├──────────────▶ approve (w=0.95)
           ├──────────────▶ observe (w=0.90)
           └──────────────▶ ar_annotate (w=0.85)
```

### 4.4 关键 API

```python
graph.entities_with_capability("patrol")   # → ["dog1", "dog2"]
graph.capabilities_for_task("st_01")       # → ["patrol", "detect"]
graph.devices_of("operator-01")            # → ["ring_01", "xr_01", ...]
graph.channels_of_device("xr_01")          # → ["spatial_view", "ar_marking", ...]
graph.available_channels("operator-01")    # → 所有在线设备的可用通道集合
```

---

## 5. 能力注册 (Capability Registry)

**文件**: `app/capability/registry.py`

### 注册机器人

```python
registry.register_entity(entity_data={
    "entity_id": "dog1",
    "type": "robot_dog",
    "capabilities": [
        {"id": "patrol", "proficiency": 0.9},
        {"id": "detect", "proficiency": 0.8, "params": {"range": 500}}
    ]
})
```

→ 创建 `HNode(entity)` + 每个能力 `HNode(capability)` + `HEdge(has_capability, weight=proficiency)`

### 注册人类操作员

```python
registry.register_human_with_devices(
    entity_id="operator-01",
    display_name="操作员张三",
    authority_level="operator",
    devices=[
        {"device_id": "ring_01", "type": "ring", "channels": ["tap_command", "haptic_alert"]},
        {"device_id": "xr_01",  "type": "xr_glasses", "channels": ["spatial_view", "ar_marking", "gaze_select"]}
    ],
    cognitive_profile={"max_concurrent_tasks": 3, "decision_accuracy": 0.95}
)
```

→ 创建实体、设备、通道节点 + `equips`/`provides` 边 → 调用 `EffectiveCapabilityResolver`

### 通道质量系数

`provides` 边权重 **不再固定为 1.0**，而是由 `_channel_quality(device_node)` 动态计算：

| 设备状态 | battery | 质量系数 |
|---------|---------|---------|
| offline | - | 0.0 |
| online | < 15% | 0.3 |
| online | < 30% | 0.7 |
| online | ≥ 30% | 1.0 |

设备状态变化时 (`update_device_status`) 自动刷新。

---

## 6. 有效能力推导 (Effective Capability Resolver)

**文件**: `app/capability/effective_resolver.py`

### 推导逻辑

人类不能直接执行 "patrol" 或 "detect"。人类的能力由 **穿戴设备提供的交互通道** 决定：

```
设备 ──provides──▶ 通道 ──enables_rules──▶ 能力
```

示例：
```yaml
# capability-ontology.yaml
enables_rules:
  approve:
    required_channels: [tap_command]
  observe:
    required_channels: [spatial_view]
  ar_annotate:
    required_channels: [spatial_view, gaze_select]
  remote_operate:
    required_channels: [spatial_view, gesture_control, force_feedback]
```

操作员 → ring_01(tap_command) + xr_01(spatial_view, gaze_select) → 推导出 approve、observe、ar_annotate

### 通道加权熟练度

`has_capability` 边的权重 = `base_proficiency × min(channel_qualities)`

- `base_proficiency`: 来自 `human-profiles.yaml` 的 `proficiency_overrides`，默认 0.8
- `channel_qualities`: 该能力所需所有通道中，`provides` 边权重的最小值（木桶原理）

```
approve 的权重 = proficiency_overrides.approve × min(ring_01.tap_command 质量)
               = 0.95 × 1.0 = 0.95

# 如果 ring_01 电量低于 15%：
approve 的权重 = 0.95 × 0.3 = 0.285  (能力大幅降低)
```

---

## 7. 任务分配 (Allocator)

**文件**: `app/capability/allocator.py`
**类型**: 定量计算节点（非 LLM）

### 7.1 整体流程

```
对每个子任务 (按优先级排序):
  │
  ├── 1. 检查实验覆盖 (ExperimentController.get_override_for_subtask)
  │      若有 → 使用实验指定的 robot/human/collaboration
  │
  ├── 2. 构建任务子超图
  │      _build_hypergraph(entities, task_plan) → 局部 HyperGraph
  │
  ├── 3. 筛选机器人候选
  │      _filter_robot_candidates(graph, subtask)
  │      → 按 required_capabilities 过滤，全部能力匹配才入选
  │
  ├── 4. 评分
  │      _score_robot(eid, subtask, graph, context) → AllocationScore
  │
  ├── 5. 确定协作模式
  │      │
  │      ├── 检查 has_capability 边上的 optimal_x (学习到的)
  │      │   若 confidence > 0.5 → _x_to_collaboration_mode(x)
  │      │
  │      ├── 否则 → 根据 risk_level / needs_human_input 确定默认模式
  │      │   risk=high → proxy (代理式)
  │      │   需要人输入 → partner (伙伴式)
  │      │   其他 → task_based (任务式)
  │      │
  │      └── x → collaboration 映射:
  │          x < 0.1  → task_based (全自主)
  │          x < 0.5  → partner (人机协同)
  │          x ≥ 0.5  → proxy (人直接操控)
  │
  ├── 6. 分配人类监督者
  │      _filter_human_supervisors(graph, collab_mode) → _score_human()
  │      尊重注意力预算 (剩余 ≥ attention_cost)
  │
  └── 7. 写入分配结果
        subtask["assigned_entity_ids"] = [best_robot]
        subtask["interaction"] = {
            collaboration, capability_mode, human_supervisor,
            bt_pattern, attention_cost
        }
```

### 7.2 机器人评分公式 (AllocationScore)

来自 `app/capability/utility.py`，权重定义在 `capability-ontology.yaml`:

```
Score = 0.30 × proficiency      # 能力匹配度 (has_capability 边权重)
      + 0.20 × proximity        # 距离分 (1 - dist/max_dist)
      + 0.15 × energy           # 电量 (battery)
      + 0.15 × availability     # 可用性 (IDLE=1, BUSY=0.3, else=0)
      + 0.10 × mode_preference  # 模式偏好 (自主型机器人偏好 task_based)
      - 0.05 × risk             # 风险惩罚
      + 0.05 × collaboration    # 协作适配
```

### 7.3 人类评分公式

```
Score = 0.30 × decision_accuracy    # 决策准确率
      + 0.20 × response_speed       # 响应速度 (1 / avg_response_sec)
      + 0.20 × authority_match      # 权限匹配度
      - 0.15 × cognitive_load       # 当前认知负荷
      - 0.15 × fatigue              # 疲劳度
```

### 7.4 注意力预算约束

```yaml
attention_budget:
  total: 1.0       # 每位操作员每个规划周期的总注意力
  reserve: 0.1     # 保留 10% 应急

# 各模式注意力开销:
task_based:  0.00   # 全自主，不消耗注意力
partner:     0.15   # 协同监督
proxy:       0.80   # 遥控操作
```

分配人类时检查: `remaining_attention ≥ attention_cost_base`。若不足则降级协作模式或跳过。

---

## 8. 行为树生成 (BT Builder)

**文件**: `app/generation/graph/bt_builder.py`
**类型**: LLM 节点
**Prompt**: `app/generation/prompts/bt_builder.md` (构建) / `bt_fix.md` (修复)

### 8.1 输入

- `task_plan`: 子任务列表 (含分配结果)
- `entities`: 实体列表
- `interaction`: 每个子任务的协作模式 + bt_pattern

### 8.2 BT 模式模板 (bt_patterns)

| 模式 | 结构 | 场景 |
|------|------|------|
| `autonomous` | `action(intent, robot)` | 全自主执行 |
| `supervised_report` | `sequence(action, report)` | 执行后汇报 |
| `human_approve_execute` | `sequence(humanGate, action)` | 人批准再执行 |
| `human_plan_execute` | `sequence(humanGate(path), action(follow))` | 人规划路径 |
| `human_remote_control` | `humanGate(remote_operate)` | 完全遥控 |

### 8.3 输出格式

```json
{
  "tree_id": "bt_bomb_search_001",
  "name": "炸弹搜索任务",
  "root_id": "root",
  "nodes": {
    "root": {
      "type": "sequence",
      "children": ["phase1_parallel", "phase2_disarm"]
    },
    "phase1_parallel": {
      "type": "parallel",
      "success_threshold": 1,
      "children": ["dog1_patrol", "dog2_patrol"]
    },
    "dog1_patrol": {
      "type": "action",
      "intent": "patrol",
      "assigned_to": "dog1",
      "execution_params": {"waypoints": [...], "loop": true}
    },
    "approve_disarm": {
      "type": "humanGate",
      "intent": "approve",
      "assigned_to": "operator-01",
      "gate_description": "确认对可疑物实施排爆",
      "timeout_sec": 60
    }
  }
}
```

### 8.4 修复循环

当 Validator 返回 FAILED，Builder 进入修复模式:
1. 接收 `violations` 列表
2. 使用 `bt_fix.md` prompt 生成修复后的 BT
3. 最多重试 `max_iterations=3` 次

---

## 9. 约束验证 (Constraint Validator)

**文件**: `app/generation/graph/constraint_validator.py`
**类型**: 规则节点

### 三重验证

| 验证器 | 文件 | 检查内容 |
|--------|------|----------|
| **能力匹配** | `validators/capability_check.py` | action 的 `intent` 必须在 assigned_to 实体的 capabilities 中 |
| **安全约束** | `validators/safety_check.py` | 离线实体不得分配任务; 关键任务需要 humanGate |
| **结构完整** | `validators/structure_check.py` | root_id 存在; 子节点引用完整; 复合节点 ≥1 子节点; 叶节点无子节点; 无环; 可达性 |

### 输出

```python
validation_report = {
    "validation_result": "PASSED" | "FAILED",
    "violations": [
        {"type": "capability_mismatch", "node": "dog1_disarm", "detail": "dog1 lacks 'disarm'"},
        {"type": "structure_error", "node": "root", "detail": "unreachable node: orphan_1"}
    ]
}
```

- `PASSED` → 进入 `fsm_bb_init`
- `FAILED` → 回到 `bt_builder` 带上 violations

---

## 10. FSM / 黑板初始化

**文件**: `app/generation/graph/fsm_bb_init.py`
**类型**: 模板节点

为执行引擎准备运行时状态:

- **FSM Definitions**: 每个实体的状态机 (IDLE → EXECUTING → COMPLETED / FAILED)
- **Blackboard Init**: 初始变量 (entity positions, battery levels, task assignments)

---

## 11. 执行引擎 (Execution Engine)

**文件**: `app/execution/engine.py`

### 执行循环

```
engine.load(bt_json, blackboard_init, fsm_defs)
engine.start(tick_period_ms=500)
  │
  ├── py_trees.BehaviourTree.tick()
  │     │
  │     ├── Action 节点 → CommandAction
  │     │     → CommandResolver.resolve(intent, entity_id, params)
  │     │     → Zenoh: zho/entity/{id}/control/action
  │     │     → 等待 action_result
  │     │
  │     ├── HumanGate 节点 → 发送人类指令
  │     │     → Zenoh: zho/directive/{operator_id}
  │     │     → 等待操作员 approve/reject
  │     │
  │     └── Condition 节点 → 读取 Blackboard 判断
  │
  ├── _check_reallocation_triggers()
  │     → 检测: 实体离线, 低电量, 认知过载, 连续失败
  │
  └── Zenoh 发布 tick 状态:
        zho/bt/execution/tick (每 tick)
        zho/bt/execution/status (状态变更)
```

---

## 12. 实验与学习基础设施

### 12.1 数据流

```
每次任务生成/执行
  │
  ▼
PerformanceCollector
  ├─ on_generation_complete: 提取 (f_i, x_i) — 任务特征 + 人参与度
  └─ on_execution_complete:  填充 P_i — 实际绩效
  │
  ▼
ExperimentStore (SQLite)
  │
  ▼
BoundaryLearner
  ├─ Phase A: fit_piecewise_linear() — 分段岭回归, 按协作模式分组
  ├─ Phase B: fit_constrained_gp()  — 双高斯过程 (目标 + 安全约束)
  │
  ├─ write_back_to_graph()  → 更新 has_capability 边的 optimal_x
  └─ write_back_to_ontology() → 更新 capability-ontology.yaml 权重
```

### 12.2 实验控制器

```
ExperimentController
  │
  ├─ load_plan(ExperimentPlan)
  │    → 定义一组 trials: 指定 robot/human/collaboration 组合
  │
  ├─ get_override_for_subtask(subtask)
  │    → Allocator 调用，若匹配则返回覆盖值
  │
  ├─ enable_auto_mode()
  │    → 用 GP suggest_next_experiment() 自动生成实验
  │    → 安全概率低于阈值 → 需人工确认
  │
  └─ confirm_pending()
       → 人工确认后执行待定实验
```

### 12.3 学习目标

找到 **人机能力边界**: 对每种任务特征组合，最优的 $x_i$（人参与度）使得绩效最大化、安全约束满足:

$$\max_{x_i, m_i} \; P_{\text{obj}}(f_i, x_i, m_i) \quad \text{s.t.} \; \Pr[P_{\text{safety}} \geq T] \geq 0.95$$

---

## 13. 动态重分配

### 触发条件

执行引擎在每个 tick 检查:

| 条件 | 阈值 | 动作 |
|------|------|------|
| 实体离线/死亡 | state = offline/dead | 重分配受影响子任务 |
| 电量过低 | battery < 0.15 | 重分配到更健康的实体 |
| 认知过载 | cognitive_load > 0.9 | 降低人的监督任务数 |
| 连续失败 | ≥ 2 次子任务失败 | 排除失败实体重新分配 |

### 重分配流程

```
Engine._check_reallocation_triggers()
  │
  ▼
main._on_reallocation_trigger()
  │
  ├── Zenoh: zho/allocation/reallocation_trigger
  │
  └── allocator.reallocate_subtask(subtask, entities, graph, exclude=[failed_entity])
        → 重新评分 → 选择新实体 → 更新行为树
```

---

## 14. 配置文件说明

### `configs/capability-ontology.yaml`

| 段落 | 说明 |
|------|------|
| `taxonomy` | 能力层级: locomotion → move → patrol; perception → detect → scan |
| `channels` | 交互通道定义: tap_command, spatial_view, gesture_control... |
| `enables_rules` | 通道 → 能力映射: {tap_command} → approve |
| `collaboration_modes` | 协作模式: task_based(自主), partner(协同), proxy(遥控) |
| `bt_patterns` | BT 结构模板: autonomous, supervised_report, human_plan_execute... |
| `utility_weights` | 评分权重: robot(proficiency=0.30...) / human(decision_accuracy=0.30...) |
| `attention_budget` | 注意力预算: total=1.0, reserve=0.1 |

### `configs/human-profiles.yaml`

操作员静态配置: 设备列表、认知画像、熟练度覆盖。当 UE5 注册消息不含设备信息时自动补全。

### `configs/pipeline.yaml`

LangGraph 管线节点顺序:
```
task_planner → allocator → bt_builder → validator(retry→bt_builder, max=3) → fsm_bb_init
```

---

## 15. Zenoh 消息拓扑

### 订阅 (HMTA 接收)

| Topic | 来源 | 用途 |
|-------|------|------|
| `zho/bt/generate/request` | IDE | 触发 BT 生成管线 |
| `zho/bt/execute/start` | IDE | 启动行为树执行 |
| `zho/bt/execute/stop` | IDE | 停止执行 |
| `zho/entity/registry` | UE5 | 实体注册 (机器人/人/设备) |
| `zho/entity/*/state` | UE5 | 实体状态更新 (位置/电量/...) |
| `zho/entity/*/control/action_result` | UE5 | 动作执行结果 |

### 发布 (HMTA 发出)

| Topic | 接收方 | 内容 |
|-------|--------|------|
| `zho/bt/generate/{task_id}/progress` | IDE | 生成进度 (步骤状态) |
| `zho/bt/generate/{task_id}/result` | IDE | 生成结果 (BT JSON) |
| `zho/bt/execution/tick` | IDE | 每 tick 状态快照 |
| `zho/bt/execution/status` | IDE | 执行状态变更 |
| `zho/entity/{id}/control/action` | UE5 | 实体控制指令 |
| `zho/directive/{id}` | IDE/XR | 人类操作指令 |
| `zho/allocation/reallocation_trigger` | IDE | 重分配通知 |

---

## 相关文档

| 文档 | 说明 |
|------|------|
| [architecture-qa.md](./architecture-qa.md) | 能力定义、可变/固定、依赖表达、FSM 与突发、BT prompt、超图边、回溯与持久化等 **Q&A** |

---

## 附录: 文件索引

| 路径 | 职责 |
|------|------|
| `app/main.py` | 应用入口, Zenoh 事件连接, 生成/执行协调 |
| `app/zenoh_bridge.py` | Zenoh 会话封装 |
| `app/capability/hypergraph.py` | HNode / HEdge / HyperGraph 数据结构 |
| `app/capability/registry.py` | 实体与能力注册管理 |
| `app/capability/effective_resolver.py` | 人类有效能力推导 |
| `app/capability/ontology.py` | capability-ontology.yaml 读取 |
| `app/capability/allocator.py` | 定量任务分配 (LangGraph 节点) |
| `app/capability/utility.py` | 评分函数 |
| `app/capability/allocation_metrics.py` | 分配质量指标 |
| `app/generation/graph/state.py` | LangGraph 状态定义 |
| `app/generation/graph/task_planner.py` | 任务分解 (LLM) |
| `app/generation/graph/bt_builder.py` | 行为树生成 (LLM) |
| `app/generation/graph/constraint_validator.py` | 约束验证 |
| `app/generation/graph/fsm_bb_init.py` | FSM / 黑板初始化 |
| `app/generation/validators/*.py` | 具体验证器 (能力/安全/结构) |
| `app/execution/engine.py` | py_trees 执行引擎 |
| `app/experiment/store.py` | 实验记录存储 (SQLite) |
| `app/experiment/learner.py` | 边界学习 (Phase A/B) |
| `app/experiment/controller.py` | 实验计划管理 |
| `app/experiment/collector.py` | 绩效数据收集 |
| `app/runtime/rule_reallocator.py` | 规则驱动重分配 |
| `app/api/*.py` | HTTP API 路由 |
| `configs/capability-ontology.yaml` | 能力本体 |
| `configs/human-profiles.yaml` | 人类操作员画像 |
| `configs/pipeline.yaml` | 管线配置 |
