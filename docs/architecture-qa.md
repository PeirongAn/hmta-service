# HMTA 架构问答（能力 · 超图 · 行为树 · FSM · 持久化）

> **位置**: `hmta-service/docs`  
> **说明**: 基于当前 HMTA 实现与 `task-allocation-architecture.md` 等文档整理的 Q&A，便于评审与 onboarding。  
> **最后更新**: 2026-03-16

---

## 1. 人、机能力分别是如何定义的？

### 机器能力（显式声明）

UE 实体注册时在 `capabilities` 字段直接列出能力 ID 与熟练度（proficiency）。HMTA 收到后创建 `entity` 节点、`capability` 节点及 `has_capability` 边，边权重即 proficiency。机器「具备哪些能力」由注册消息确定。

### 人类能力（间接推导）

人本身不直接注册能力列表，而是注册（或由 `human-profiles.yaml` 补全）**穿戴设备**；设备通过 `provides` 提供**交互通道**；`capability-ontology.yaml` 中的 `enables_rules` 规定「哪些通道组合可解锁哪些能力」。推导链可概括为：

`人 → equips → 设备 → provides → 通道 → enables_rules → 能力`

### 共同基础：本体（Ontology）

`capability-ontology.yaml` 的 `taxonomy` 定义能力层级、别名，并扩展 **Skill Model** 相关字段：`input_schema`、`preconditions`、`effects`、`ue_action_type` 等，供规划、BT 生成与执行层共用。

---

## 2. 人和机的能力：可变与不可变部分分别是什么？

### 机器

| 类别 | 内容 |
|------|------|
| **相对不可变** | 能力集合本身（注册时声明，例如 patrol/detect；不会在无重新注册的情况下「凭空」多出 disarm） |
| **可变** | `has_capability` 边权重受运行态影响（如电量、通信/可用性）；实体状态（idle/busy/offline 等）影响分配评分；实验学习可向边 `attrs` 写入 `optimal_x` 等 |

### 人类

| 类别 | 内容 |
|------|------|
| **相对不可变** | `human-profiles.yaml` 中的画像：`cognitive_profile`、`proficiency_overrides` 等（设计态、短期视为稳定） |
| **可变** | 设备上下线导致**有效能力集合**与 `has_capability` 边增删；`provides` 通道质量（电量等）改变边权重；运行时认知负荷、疲劳等影响人类评分；若本体配置了 `channel_quality_map`，同一技能在不同通道组合下可有不同质量系数 |

---

## 3. 能力调用依赖关系是如何表达的？

当前依赖分布在**多个层次**，而非单一「依赖边」穷举：

1. **任务级**：Task Planner 输出的 `TaskPlan` 中，子任务 `dependencies` 描述先后/前置子任务关系。
2. **技能级（本体）**：`preconditions` / `effects` 表达「执行前需满足的状态」与「成功后产生的状态」；执行层可通过 `PreconditionCheck`、黑板写入 effects 形成数据流链。
3. **参数级**：`input_schema` 的 `source`（如 `human|preset`、`runtime`）表达参数从何处来、是否依赖人机或上游结果。

**说明**：超图里目前没有单独的 `depends_on` 边类型；任务依赖、本体前置/效果、BT 的 sequence/selector 结构共同承担「谁依赖谁」的表达。若需更强校验，可考虑在超图中增加显式依赖边。

---

## 4. 如何用有限 FSM 状态应对无数可能性（含突发）？

策略是**分层**，而不是用一个巨大 FSM 覆盖所有世界状态：

| 层次 | 作用 |
|------|------|
| **实体 FSM（粗粒度）** | 如 idle / executing / completed / failed 等，提供基线状态感知，供分配与可用性判断 |
| **行为树（py_trees）** | selector/sequence 等提供反应式组合；每 tick 重评，适合「先试 A 再试 B」类逻辑 |
| **动态重分配** | 引擎周期性检查触发条件（离线、低电量、认知过载、连续失败等），触发后重新分配子任务，**不依赖**预定义所有转移边 |
| **参数与人机回路** | 缺参时进入 `waiting_params`，通过 Zenoh/IDE 请求人工补参，避免「一缺参就死」 |

**局限**：`preconditions` 仍依赖人工/设计枚举；完全未预料的突发（如发现新目标、任务目标变更）通常仍需重规划、人工介入或上层策略扩展，而不是单靠 FSM 状态数增长解决。

---

## 5. 行为树当前借助的 prompt 是什么？后续可如何优化？

### 当前（BT Builder）

- **主模板**：`app/generation/prompts/bt_builder.md`  
- **修复**：验证失败时用 `bt_fix.md`  
- 典型内容包括：输出 JSON Schema、节点类型说明、与 Allocator 给出的 `bt_pattern` 对齐的规则、终止条件、few-shot 示例，以及从本体注入的 **preconditions/effects** 与 **input_schema** 摘要。

### 后续优化方向（建议）

1. **更强结构化输入**：把 task_planner 的依赖图（偏序）显式注入，减少 LLM 自由发挥导致的并行/串行错误。  
2. **修复质量**：除 `violations` 外，可积累「违规类型 → 成功修复模式」的示例库，做检索式 few-shot。  
3. **确定性拼装**：在 subtask、bt_pattern、依赖已知的前提下，逐步让**规则引擎**承担大部分 BT 拼装，LLM 只负责少数开放性决策。  
4. **参数占位**：在 prompt 中强制对每个 action 核对 `input_schema` 的必填项，并说明从上下文如何填值，降低空 `execution_params`。

---

## 6. 超图中哪些边是静态/动态的？查询能力时用吗？

| 边类型 | 静态性 | 典型变化时机 | 查询能力时 |
|--------|--------|----------------|------------|
| `equips`（人↔设备） | 半静态 | 设备注册/下线 | **用**：人类能力推导起点 |
| `provides`（设备↔通道） | 动态 | 电量、在线状态等 | **用**：通道质量影响有效能力权重 |
| `has_capability`（实体↔能力） | 半静态~动态 | 注册、设备变化、学习回写 attrs | **用**：分配器 proficiency、实体筛选 |
| `requires`（任务↔能力） | 临时 | 每次任务规划 | **用**：子任务需要哪些能力 |
| `collaborates`（实体↔实体） | 临时 | 分配过程 | 间接用于协作相关评分 |
| `enables`（通道→能力） | 多在本体规则中实现 | ontology 重载 | 逻辑上等价于 enables_rules，不一定以边形式存储 |

**查询路径简述**：机器直接查 `has_capability`；人类经 `equips` → `provides` → `enables_rules` 推导后再体现为 `has_capability`；任务侧用 `requires` 与 `entities_with_capability` 等 API 匹配。

---

## 7. 回溯过程改了超图中的哪些值？

需区分「BT 修复循环」与「学习/运行态更新」：

- **Validator 失败 → 仅重跑 BT Builder**：一般不修改超图结构或边权，只重生成 BT JSON。  
- **BoundaryLearner `write_back_to_graph()`**：更新 `has_capability` 边的 **`attrs`**（如 `optimal_x`、`optimal_x_confidence`），用于后续 Allocator 在置信度足够时映射协作模式；不必然改变 proficiency 主权重。  
- **动态重分配 / 设备状态更新**：可能更新 `provides` 权重、人类侧 `has_capability` 边的增删与权重、以及与任务相关的临时边信息。

---

## 8. 持久化存储了吗？存了什么？

### 已持久化（实验与学习）

- **SQLite**（`app/experiment/store.py`）：实验与绩效样本，通常包含任务/子任务/实体标识、特征向量（如复杂度、风险、时间压力、切换成本等）、人机参与度、协作模式、绩效与安全等指标及时间戳。  
- 用途：Phase A/B 学习、边界拟合、（可选）回写到图或本体。

### 当前多未持久化（需注意）

- **超图全量**：进程内为主，重启后依赖 UE/注册消息重建。  
- **BT JSON / 黑板 / 实体 FSM 细粒度运行态**：随运行结束或服务重启丢失，除非另行做 checkpoint 设计。  
- **单次分配结果**：随生成管线状态与消息传递，默认不落库。

若需生产级容灾，可考虑：超图快照、最近一次 BT + 关键黑板 checkpoint、分配与执行事件日志等（属产品化扩展，非当前最小实现范围）。

---

## 相关文档

- [任务分配架构全景](./task-allocation-architecture.md)  
- [超图说明](./hypergraph.md)  
- [任务分配流水线](./task-allocation-pipeline.md)
