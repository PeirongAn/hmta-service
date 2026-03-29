# 找炸弹 · 第一阶段演示数据

## 实体与能力

- `demo-bomb-search-entities.yaml`：2 机器狗（`dog1` / `dog2`）+ 1 操作员（高 `approve`、中 `path_planning`，与 `human-profiles.yaml` 中 `operator-01` 一致）。
- `demo-bomb-search-entities-contrast.yaml`：相同狗与任务，仅操作员 **proficiency 对照**（低 `approve`、高 `path_planning`），用于同一 `task_context.objective` 下对比 **allocation_trace**。

## 生成请求示例（HTTP）

将 YAML 中 `entities` / `environment` / `task_context` 合并进 JSON，POST `http://localhost:8000/api/v1/generate`：

```json
{
  "task_id": "demo-bomb-1",
  "entities": [ ... ],
  "environment": { "scene": "L_Convolution_Blockout" },
  "task_context": { "objective": "..." }
}
```

## 响应与追溯

成功时 `GenerationTaskRecord`（及 Zenoh `zho/bt/generate/{task_id}/result`）包含：

- `task_plan` — 子任务与 `interaction`（allocator 写入）
- `allocation_trace` — 每子任务候选机器人评分、指派、`human_supervisor`
- `allocation_quality` — 聚合指标
- `capability_graph` — 超图快照（供 IDE 人机物面板）

## UE 注册

- 机器狗：`entity_id` 与 `dog1`/`dog2` 对齐（或与生成请求中一致）。
- 操作员：`metadata.profile` 设为 `operator-01` 或 `operator-01-alloc-contrast`，可由 HMTA 从 `human-profiles.yaml` 补全设备与认知字段。

## 威胁物进入泳道「物」列

UE 通过 `zho/entity/*/event` 发送事件，例如 `eventType: bomb_detected`，负载中可带 `threatEntityId`、`position`、`confidence`；zho-core 转发后，IDE 会为威胁注册 `object` 节点并写入泳道时间线。
