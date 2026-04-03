# 任务队列已知问题追踪

> 发现时间：2026-03-31
> 场景：机器狗巡逻任务（bomb-search demo，dog1 + dog2）
> 状态：待修复

---

## Issue #1：Allocator 覆盖 LLM 预分配导致实体交叉 [Critical]

**现象**：dog1 的任务队列中出现 dog2 的任务，dog2 队列中出现 dog1 的任务。

**根因**：Allocator 在评分分配时，无条件覆盖 TaskPlanner (LLM) 已设置的 `assigned_entity_ids`，且 proximity 评分因缺失位置数据而失效，导致最终分配结果与 LLM 的分区规划不一致。

**关键代码路径**：

| 步骤 | 文件 | 行号 | 行为 |
|------|------|------|------|
| LLM 规划 | `app/generation/graph/task_planner.py` | 30 | SubTask 输出包含 `assigned_entity_ids` |
| 覆盖分配 | `app/capability/allocator.py` | 528-529 | `subtask["assigned_entity_ids"] = [best_robot.entity_id]` 直接覆盖 |
| 位置失效 | `app/capability/utility.py` | 225-226 | entity 无 `position`，proximity 恒返回 0.5 |
| 优先级排序 | `app/capability/allocator.py` | 382-385 | `subtasks_sorted` 改变迭代顺序 |
| 负载均衡 | `app/capability/allocator.py` | 410-414 | 每分配一个任务 -0.05，改变后续评分结果 |

**复现路径**：

1. 使用 `configs/demo-bomb-search-entities.yaml`（两只狗无 position 字段）
2. 发起生成请求，LLM 规划出按区域分配的子任务
3. Allocator 按优先级排序后重新评分，dog1 proficiency 略高先被选中
4. 第二个任务因负载惩罚翻转给 dog2 → 分配与 LLM 规划相反

**修复方向**：

- **方案 A**：Allocator 尊重 LLM 预分配作为评分加权（hint bonus），而非忽略
- **方案 B**：在实体配置中补充 `position` 字段使 proximity 评分生效
- 建议两者都做

---

## Issue #2：任务队列未做拓扑排序 [Medium]

**现象**：任务队列按 LLM 返回的原始顺序构建，即使存在 `depends_on` 依赖关系也不重排。

**影响**：如果 LLM 返回的顺序恰好把被依赖任务排在后面（例如 B 在 A 前面，但 B depends_on A），`_pick_next_task()` 需要多轮扫描才能找到可执行任务，极端情况下可能导致不必要的 skip。

**关键代码**：

- `app/generation/graph/fsm_bb_init.py:296-354` — `_build_task_queue()` 按原始顺序遍历 subtasks 构建队列，无拓扑排序
- `app/execution/behaviours/entity_worker.py:435-455` — `_pick_next_task()` 线性扫描，返回第一个满足依赖的任务

**修复方向**：在 `_build_task_queue()` 构建队列后，按 `depends_on` 做拓扑排序再返回。

---

## Issue #3：任务选取不考虑优先级 [Medium]

**现象**：`_pick_next_task()` 返回第一个依赖满足的 pending 任务，不考虑 priority 字段。

**影响**：当多个任务同时依赖满足时，低优先级任务可能先于高优先级任务执行。

**关键代码**：

- `app/execution/behaviours/entity_worker.py:435-455` — 线性扫描 FIFO

**示例**：
```
队列: [task_normal_1(pending), task_critical_2(pending), task_normal_3(pending)]
所有依赖已满足 → 选中 task_normal_1，而非 task_critical_2
```

**修复方向**：收集所有依赖满足的 pending 任务，按 priority 排序后选最高优先级。

---

## Issue #4：Allocator 优先级排序结果未回传 [Low]

**现象**：Allocator 内部按 priority 对 subtasks 排序用于评分（`subtasks_sorted`），但返回的 `task_plan` 仍是原始顺序。

**关键代码**：

- `app/capability/allocator.py:382-385` — 创建 `subtasks_sorted`
- `app/capability/allocator.py:591` — 返回原始 `task_plan`（未更新排序）

**影响**：下游节点（`fsm_bb_init`）收到的 subtask 顺序与 Allocator 处理顺序不一致。如果 Issue #2 修复了拓扑排序，此问题影响降低。

**修复方向**：Allocator 返回前将 `task_plan["subtasks"]` 更新为排序后的列表。

---

## Issue #5：Coverage Ensurer 自动补充任务始终追加到末尾 [Low]

**现象**：当 CoverageEnsurer 检测到未覆盖区域并自动生成补充任务时，这些任务一律 `append` 到 subtask 列表末尾。

**关键代码**：

- `app/generation/graph/coverage_ensurer.py:180-192`

**影响**：自动补充的任务可能在逻辑上应更早执行（如优先覆盖高危区域），但始终排在最后。在 Issue #2（拓扑排序）和 Issue #3（优先级选取）修复后影响可降低。

---

## 优先级总结

| Issue | 严重程度 | 用户可感知 | 建议修复顺序 |
|-------|---------|-----------|-------------|
| 实体交叉分配 | Critical | 是（dog1/dog2 任务反了） | 1 |
| 无拓扑排序 | Medium | 偶发 | 2 |
| 不考虑优先级 | Medium | 多任务时可感知 | 3 |
| 排序未回传 | Low | 间接 | 4 |
| 补充任务在末尾 | Low | 间接 | 5 |