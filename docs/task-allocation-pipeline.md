# HMTA 任务分配流程文档

> Human-Machine Task Allocation Service — 完整流程说明
>
> 对应代码路径: `hmta-service/app/`

---

## 总览

HMTA Service 的任务分配分为两大阶段：

| 阶段 | 引擎 | 职责 |
|------|------|------|
| **设计态 — Generation** | LangGraph (5 节点流水线) | 将自然语言任务目标转化为可执行的行为树 + FSM + Blackboard |
| **运行态 — Execution** | py_trees (Tick Loop) | 确定性执行行为树，驱动机器人指令与人类指令 |

两阶段之间通过 Zenoh 消息总线衔接，完整流程如下：

```
┌──────────────────────── Generation (设计态) ────────────────────────┐
│                                                                     │
│  请求 → ① 任务分解 → ② 能力分配 → ③ 行为树构建 → ④ 约束验证 → ⑤ FSM/BB 初始化  │
│              │            │            │            │                │
│              ▼            ▼            ▼         失败→回③            │
│          task_plan   allocation    BT JSON     (最多3轮)            │
│                                                                     │
└───────────── 产物: BT + FSM + BB → 加载到执行引擎 ──────────────────┘
                                        │
                                        ▼
┌──────────────────────── Execution (运行态) ─────────────────────────┐
│                                                                     │
│  ⑥ 执行引擎加载 → ⑦ Tick 循环 → ⑧ 指令分发 → ⑨ 人机交互 → ⑩ 完成/干预  │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 步骤详解

### ① 任务分解 (Task Planner)

| 属性 | 值 |
|------|---|
| 节点类型 | LLM |
| 源码 | `generation/graph/task_planner.py` |
| Prompt | `generation/prompts/task_planner.md` |

**输入**：
- `entities` — 场景中所有实体的基础信息（entity_id, type, status, capabilities）
- `environment` — 环境描述（区域、地形、威胁等）
- `task_context` — 任务上下文（目标、约束、作战条令等）

**处理**：
1. 将实体列表精简为仅包含 base fields + capabilities（去除设备扩展信息），避免 LLM 上下文干扰
2. 调用 LLM（通过 LangChain LCEL chain），将任务目标分解为 phases（阶段）和 subtasks（子任务）
3. LLM 为每个子任务初步指定 `assigned_entity_ids`、`required_capabilities`、`dependencies`

**输出** — `task_plan`：
```json
{
  "phases": [...],
  "subtasks": [
    {
      "subtask_id": "st_01",
      "description": "侦察 A 区",
      "assigned_entity_ids": ["uav-01"],
      "required_capabilities": ["reconnaissance", "navigate"],
      "dependencies": []
    }
  ],
  "constraints": [...],
  "doctrine_notes": "..."
}
```

---

### ② 能力分配 (Allocator)

| 属性 | 值 |
|------|---|
| 节点类型 | 量化规则 (quantitative) |
| 源码 | `capability/allocator.py`、`capability/utility.py`、`capability/hypergraph.py` |
| 配置 | `configs/capability-ontology.yaml`（权重、协作模式、注意力预算） |

这是整个流程中最核心的步骤，用量化评分替代 LLM 的模糊分配。

#### 2.1 构建超图 (Hypergraph)

为本次任务构建一个任务作用域的超图，节点包含 5 种类型：

| 节点类型 | 说明 | 来源 |
|----------|------|------|
| `entity` | 机器人 / 人类操作员 | 实体注册表 |
| `device` | 人类佩戴的设备（指环、XR眼镜、手套等） | human-profiles.yaml |
| `channel` | 设备提供的交互通道（视觉、触觉、语音等） | 设备 → 通道映射 |
| `capability` | 能力节点（导航、侦察、路径规划等） | 实体能力 + 通道推导 |
| `task` | 子任务节点 | Task Planner 输出 |

超边类型：
- `equips` — 人 → 设备
- `provides` — 设备 → 通道
- `enables` — 通道 → 能力（通过 `EffectiveCapabilityResolver` 推导）
- `has_capability` — 实体 → 能力（机器人直接关联，人类通过设备链推导）
- `requires` — 子任务 → 能力

#### 2.2 按优先级排序子任务

将子任务按优先级排序：`critical > urgent > normal`，确保高优先级任务优先锁定资源。

#### 2.3 逐个子任务分配

对每个子任务，按以下 5 步执行：

**Step 1 — 筛选机器人候选** (`_filter_robot_candidates`)
- 排除离线/已阵亡实体
- 排除类型为 human 的实体
- 检查实体能力是否覆盖子任务所有 `required_capabilities`

**Step 2 — 机器人评分** (`compute_robot_utility`)

量化维度及默认权重：

| 维度 | 权重 | 计算方式 |
|------|------|----------|
| proficiency (能力熟练度) | 0.30 | 超图 has_capability 边权重的加权平均 |
| proximity (距离) | 0.20 | 与目标位置的欧氏距离归一化 |
| energy (电量) | 0.15 | battery / 100 |
| availability (可用性) | 0.15 | idle=1.0, 其他=0.2 |
| mode_preference (模式偏好) | 0.10 | autonomous=1.0, supervised=0.6, remote=0.3 |
| risk (风险) | -0.05 | 实体当前风险值（负权重 = 越高越差） |
| collaboration (协作增益) | 0.05 | 是否属于协作组 |

选取最高分的机器人。

**Step 3 — 确定协作模式** (`_required_collab_mode`)

遍历子任务 required_capabilities 关联的 has_capability 边，取最苛刻的 `collab_mode`：

| 优先级 | 协作模式 | BT 模式 | 说明 |
|--------|---------|---------|------|
| 最高 | `proxy` | `human_full_control` | 人类遥控 |
| 中 | `partner` | `human_plan_execute` | 人类规划、机器人执行 |
| 最低 | `task_based` | `autonomous` | 全自主 |

如果能力参数需要人类输入（如路径规划），会自动从 `autonomous` 升级到 `human_plan_execute`。

**Step 4 — 筛选并评分人类监督者** (`_filter_human_supervisors` + `compute_human_utility`)

仅在协作模式不是 `task_based` 或需要人类输入时触发。

筛选条件：
- 必须为 human 类型且在线
- 设备通道必须满足协作模式的最低通道要求 (`min_channels`)
- 剩余注意力预算足够承担该子任务的注意力消耗

人类评分维度：

| 维度 | 权重 | 说明 |
|------|------|------|
| decision_accuracy (决策准确率) | 0.30 | 历史表现或默认 0.8 |
| response_speed (响应速度) | 0.20 | 平均响应秒数归一化 (0s→1.0, 30s→0.0) |
| authority_match (权限匹配) | 0.20 | 是否持有相应权限等级 |
| cognitive_load (认知负荷) | -0.15 | 当前任务数 / 最大并行任务数（负权重） |
| fatigue (疲劳度) | -0.15 | 当前疲劳值（负权重） |

选取最高分的人类操作员作为 `human_supervisor`。若无合格人类，降级为 `task_based` + `autonomous`。

**Step 5 — 写入分配结果**

将分配结果写回子任务的 `interaction` 字段：

```json
{
  "collaboration": "partner",
  "capability_mode": "MODE_SUPERVISED",
  "human_supervisor": "operator-01",
  "attention_cost": 0.35,
  "bt_pattern": "human_plan_execute"
}
```

#### 2.4 输出

- 更新后的 `task_plan`（每个子任务含 `assigned_entity_ids` + `interaction`）
- `capability_graph` — 超图序列化快照
- `allocation_trace` — 每个子任务的候选列表、评分、选择原因

---

### ③ 行为树构建 (BT Builder)

| 属性 | 值 |
|------|---|
| 节点类型 | LLM |
| 源码 | `generation/graph/bt_builder.py` |
| Prompt (构建) | `generation/prompts/bt_builder.md` |
| Prompt (修复) | `generation/prompts/bt_fix.md` |

**输入**：
- 每个实体的 capabilities 列表
- 分配完成后的 `task_plan`（含 `interaction` 块）

**处理**：
1. 首次构建 (build)：将任务计划 + 实体能力 → LLM 生成 BT JSON
2. 修复模式 (repair)：如果来自验证器的回退，则携带当前 BT + violations，LLM 仅修复违规节点

LLM 根据 `bt_pattern` 决定行为树模式：
- `autonomous` → 纯 action 节点链
- `human_plan_execute` → humanGate → action 序列
- `human_full_control` → humanGate 包裹所有操作

**输出** — `behavior_tree`：
```json
{
  "tree_id": "bt_task_001",
  "root_id": "root",
  "nodes": {
    "root": { "type": "sequence", "children": [...] },
    "action_01": { "type": "action", "intent": "navigate", "entity": "ugv-01", "params": {...} },
    "gate_01": { "type": "humanGate", "entity": "operator-01", "intent": "command.approve", ... }
  },
  "metadata": {}
}
```

支持的节点类型：`sequence`, `selector`, `parallel`, `condition`, `action`, `humanGate`, `timeout`, `retry`

---

### ④ 约束验证 (Constraint Validator)

| 属性 | 值 |
|------|---|
| 节点类型 | 纯规则 (rule)，无 LLM |
| 源码 | `generation/graph/constraint_validator.py` |
| 验证器 | `generation/validators/capability_check.py`, `safety_check.py`, `structure_check.py` |

执行三类验证：

| 验证类别 | 检查内容 |
|---------|---------|
| **Capability Match** | action 节点的 intent 是否在分配实体的能力集内；humanGate 实体是否为 human |
| **Safety** | 离线实体不可执行关键任务；critical 级任务必须有 humanGate |
| **Structure** | 根节点存在；所有 children 引用有效；无循环；所有节点可达 |

**判定逻辑**：
- 0 violations → `PASSED` → 流向步骤 ⑤
- >0 violations → `FAILED` → 回退到步骤 ③ (BT Builder repair)，最多 3 轮

```
bt_builder ←──── validator (FAILED, iteration < 3)
    │                │
    │                └── validator (PASSED) → fsm_bb_init
    │
    └── validator (FAILED, iteration = 3) → 生成失败
```

---

### ⑤ FSM & Blackboard 初始化 (FSM/BB Init)

| 属性 | 值 |
|------|---|
| 节点类型 | 模板 (template)，无 LLM |
| 源码 | `generation/graph/fsm_bb_init.py` |

**FSM 定义生成**：
为每个实体创建 FSM 定义（初始状态 = `idle`）：
```json
{ "entity_id": "ugv-01", "entity_type": "robot", "initial_state": "idle" }
```

**Blackboard 初始化**：
从三个来源收集初始键值：
1. **实体基线** — `entities/{id}/status`, `entities/{id}/comm_status`, `entities/{id}/position`, `entities/{id}/fsm_state`
2. **环境区域** — `zones/{zone_id}/explored`, `zones/{zone_id}/cleared`, `zones/{zone_id}/data`
3. **BT condition 节点** — 扫描所有 condition 节点的 `key` + `expected`，预注册键位

**输出**：
- `fsm_definitions` — 实体 FSM 定义列表
- `blackboard_init` — 初始 Blackboard 键值映射

---

### ⑥ 执行引擎加载 (Engine Load)

| 源码 | `execution/engine.py` → `ExecutionEngine.load()` |
|------|---|

Generation 流水线完成后，产物（BT JSON + FSM Defs + BB Init）被自动加载到执行引擎：

1. **Blackboard 初始化** — 注册结构键（zones、condition keys），跳过实体运行态键（由 BlackboardSync 管理）
2. **构建 py_trees 树** — `tree_loader.load_tree()` 将 JSON 转为 py_trees 行为节点
3. **加载 FSM** — `FSMManager.load_definitions()` 创建每个实体的状态机实例
4. **启动 BlackboardSync** — 订阅 Zenoh 实体状态 → 实时同步到 Blackboard
5. **挂载 SnapshotPublisher** — 每 tick 向前端推送节点状态快照
6. **挂载 ProfilerPublisher** — 推送甘特图/瓶颈数据
7. **依赖注入** — 递归为所有 `CommandAction` / `HumanGate` 节点注入 `CommandResolver` 和 `ZenohBridge`

加载完成后发布 `execution_status: "loaded"`，等待前端发送 start 指令。

---

### ⑦ Tick 循环 (Tick Loop)

| 源码 | `execution/engine.py` → `ExecutionEngine.start()` |
|------|---|

前端通过 Zenoh 发送 `zho/bt/execute/start` 后，引擎在后台线程启动 tick 循环（默认 10Hz = 100ms/tick）：

**每个 Tick 的执行顺序**：

1. **Pre-tick** — `FSMManager.sync_to_blackboard()` 将所有实体 FSM 当前状态写入 Blackboard
2. **Tick** — py_trees 从根节点开始评估行为树
3. **Post-tick**：
   - `SnapshotPublisher` → 发布每个节点的状态到 `zho/bt/execution/tick`
   - `ProfilerPublisher` → 发布甘特图数据
   - 检查根节点状态：若 SUCCESS 或 FAILURE → 发布完成状态并停止引擎

---

### ⑧ 指令分发 (Command Dispatch)

#### 三层指令模型

```
L1: AbstractCommand (意图层)
    ↓
L2: RobotCommand / HumanDirective (类型层)
    ↓
L3: UE Payload / Zenoh Message (传输层)
```

#### CommandAction (机器人/人类动作节点)

源码：`execution/behaviours/command_action.py`

py_trees `action` 节点的执行生命周期：

1. **initialise()** — 首次进入 RUNNING 时调用一次：
   - 从 Blackboard 读取人类提供的参数（如 `planned_waypoints`）
   - 构建 `AbstractCommand`（intent + entity_id + params + node_id）
   - 调用 `CommandResolver.resolve()` 分发

2. **update()** — 每个 tick 调用：
   - 轮询 `CommandResolver.get_status()` → `completed` / `failed` / `running`
   - 映射到 py_trees 的 `SUCCESS` / `FAILURE` / `RUNNING`

3. **terminate()** — 被中断时取消进行中的指令

#### CommandResolver 路由逻辑

源码：`execution/command/command_resolver.py`

```
AbstractCommand
  ├── 查验实体存在性 → ENTITY_NOT_FOUND
  ├── 查验能力匹配 → CAPABILITY_MISMATCH
  ├── 查验在线状态 → ENTITY_OFFLINE / COMM_LOST
  │
  ├── entity_type == "robot"
  │   └── RobotTranslator.translate() → RobotCommand
  │       └── ue_adapter.to_ue_payload() → Zenoh: zho/entity/{id}/control/action
  │
  └── entity_type == "human"
      └── HumanTranslator.translate() → HumanDirective
          └── Zenoh: zho/directive/{entity_id}
```

#### HumanTranslator — 人类指令翻译

源码：`execution/command/human_translator.py`

根据 intent 映射指令类型：

| Intent | Directive Type | 预设选项 |
|--------|---------------|---------|
| `command.approve` | approval | 批准 / 拒绝 / 附条件批准 / 请求重新验证 |
| `plan_path` | path_planning | 提交路径点 / 拒绝 / 修改参数后提交 |
| `observe` | observation | 提交观察报告 / 无法执行 |
| `command.override` | override | 切换人工控制 / 保持自动模式 |

翻译过程还包括：
- 从 Blackboard 读取态势信息构建 `situation_briefing`
- 根据操作员权限等级过滤可用选项（operator / supervisor / commander）
- 生成推荐操作建议

---

### ⑨ 人机交互 (Human Interaction)

#### HumanGate (人类审批节点)

源码：`execution/behaviours/human_gate.py`

人类决策等待节点的执行逻辑：

1. **initialise()** — 通过 CommandResolver 发送 HumanDirective 给操作员
2. **update()** — 每 tick 检查：
   - 在超时窗口的 50%、75%、90% 自动发送催促提醒
   - 若超时且重试次数未耗尽 → 重新发送指令（默认最多重试 2 次）
   - 所有重试耗尽 → FAILURE
   - 收到响应 `completed` → SUCCESS
   - 收到响应 `failed` → FAILURE
3. **terminate()** — 被中断时取消进行中的指令

#### ResponseResolver (响应处理)

源码：`execution/command/response_resolver.py`

操作员在前端做出选择后，响应通过 Zenoh `zho/response/*` 到达 `HumanAdapter` → `ResponseResolver`：

| 操作员选项 | Blackboard 更新 | 指令状态 |
|-----------|----------------|---------|
| `approve` / `proceed` | `approval_result = "approved"` | completed |
| `reject` / `abort` | `approval_result = "rejected"`, `rejection_reason = ...` | failed |
| `approve_with_conditions` | `approval_result = "approved"`, `approval_conditions = ...` | completed |
| `submit_waypoints` | `approval_result = "approved"`, `planned_waypoints = [...]` | completed |
| `request_recheck` | `recheck_requested = true` | failed |

Blackboard 更新会立即被下一个 tick 中的 action 节点感知到（如 `CommandAction` 读取 `planned_waypoints`）。

---

### ⑩ 完成与干预

#### 指令完成检测

两种完成机制并行运行：

| 机制 | 来源 | Topic |
|------|------|-------|
| **显式回调** | UE 发送 action_result | `zho/entity/*/control/action_result` |
| **状态推断** | BlackboardSync 监测实体从 busy → idle | `zho/entity/*/state` |

`CommandResolver.complete_by_action_result()` 优先按 `nodeId` 匹配，回退到 `entity_id` 匹配。

#### 人工干预 (Human Intervention)

源码：`execution/human_interaction/`

运行态支持以下人工干预操作：

| 操作 | Handler | 说明 |
|------|---------|------|
| 暂停 | `InterventionHandler` | 暂停 tick 循环 |
| 恢复 | `InterventionHandler` | 恢复 tick 循环 |
| 接管 | `InterventionHandler` | 切换到完全人工控制 |
| 重分配 | `EditHandler` | 修改 BT 节点的 entity 指派 |
| 添加/删除/修改节点 | `EditHandler` | 热编辑行为树结构 |
| 重新生成 | `EditHandler` | 重新触发 Generation 流水线 |

#### 行为树完成

当 py_trees 根节点返回 SUCCESS 或 FAILURE：
- 发布 `execution_status: "completed"` 或 `"failed"`
- 停止 tick 循环
- `ProfilerPublisher` 生成最终的甘特图数据

---

## 关键 Zenoh Topic

| Topic | 方向 | 阶段 | 用途 |
|-------|------|------|------|
| `zho/bt/generate/request` | Theia → Service | 设计态 | 触发 Generation 流水线 |
| `zho/bt/generate/{id}/progress` | Service → Theia | 设计态 | 流水线步骤进度 |
| `zho/bt/generate/{id}/result` | Service → Theia | 设计态 | 最终产物 (BT + FSM + BB) |
| `zho/bt/execute/start` | Theia → Service | 运行态 | 启动 tick 循环 |
| `zho/bt/execute/stop` | Theia → Service | 运行态 | 停止 tick 循环 |
| `zho/bt/execution/tick` | Service → Theia | 运行态 | 每 tick 节点状态快照 |
| `zho/bt/execution/status` | Service → Theia | 运行态 | 总体执行状态 |
| `zho/entity/{id}/control/action` | Service → UE | 运行态 | 机器人指令 |
| `zho/entity/{id}/control/action_result` | UE → Service | 运行态 | 动作结果回调 |
| `zho/directive/{entity_id}` | Service → Theia | 运行态 | 人类指令卡片 |
| `zho/response/*` | Theia → Service | 运行态 | 操作员响应 |
| `zho/entity/registry` | UE → Service | 启动 | 实体注册 |
| `zho/entity/*/state` | UE → Service | 运行态 | 实体运行态遥测 |

---

## 配置文件

| 文件 | 用途 |
|------|------|
| `configs/pipeline.yaml` | LangGraph 流水线节点顺序、重试配置 |
| `configs/capability-ontology.yaml` | 能力分类树、通道定义、enables 规则、协作模式、评分权重、注意力预算 |
| `configs/human-profiles.yaml` | 操作员设备配置（指环/XR/手套等）、认知画像 |

---

## 流水线状态流转

```
GenerationState (TypedDict)
├── 输入: task_id, entities, environment, task_context
├── ① task_planner 写入: task_plan
├── ② allocator 写入: task_plan (更新), capability_graph, allocation_trace
├── ③ bt_builder 写入: behavior_tree, iteration_count++
├── ④ validator 写入: validation_report, violations
├── ⑤ fsm_bb_init 写入: fsm_definitions, blackboard_init
└── 全程追加: generation_trace (每步的 step/status/elapsed_ms)
```
