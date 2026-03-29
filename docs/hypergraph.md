当前超图（HyperGraph）全貌
一、数据模型
超图是一种扩展图结构，每条超边可以连接任意数量的节点。由 hmta-service/app/capability/hypergraph.py 实现。

5 种节点类型（HNode.kind）：

kind	含义	示例
entity	执行者（人或机器人）	dog1, operator-01
capability	能力	follow_by_path, approve, path_planning
task	子任务	patrol_zone_A
device	穿戴设备	ring_01, xr_01, glove_01
channel	交互通道	tap_command, spatial_view, gesture_control
6 种超边类型（HEdge.kind）：

kind	连接关系	weight 含义
has_capability	entity ↔ capability	熟练度 (proficiency)
requires	task ↔ capabilities	重要度 (importance)
collaborates	{entities} ↔ task	协同增益 (synergy)
equips	entity ↔ device	1.0 (佩戴关系)
provides	device ↔ channel	1.0 (设备提供通道)
enables	{channels} ↔ capability	通道组合解锁能力
二、运行时超图的实际结构
以当前系统中注册的实体为例，超图大致长这样：

                        ┌─────────────┐
                        │  operator-01│ (entity, human)
                        └──────┬──────┘
                   ┌───────────┼───────────┐
                equips      equips      equips
                   │           │           │
              ┌────▼────┐ ┌───▼────┐ ┌───▼─────┐
              │ ring_01 │ │ xr_01  │ │glove_01 │ (device)
              └────┬────┘ └───┬────┘ └───┬─────┘
             provides    provides    provides
            ┌───┴───┐  ┌──┬──┴──┬──┐ ┌──┴──┬──────┬──────┐
            │       │  │  │     │  │ │     │      │      │
        tap_cmd  haptic spatial ar gaze gesture hand  force
                        view  mark sel  control track feedback
            (channel)  (channel)      (channel)
        tap_command ──┐ enables ──→ approve     ─┐
                      └ enables ──→ quick_command│ has_capability
                                                 ├──→ operator-01
        spatial_view + gaze_select ── enables ──→ ar_annotate    │
                                                 │
        spatial_view + gesture_control ── enables → path_planning│
                                                 │
        spatial_view + gesture_control            │
          + force_feedback ── enables ──→ remote_operate ─┘
        ┌───────┐  has_capability(prof=0.9)  ┌───────────────┐
        │ dog1  │ ─────────────────────────→ │follow_by_path │
        │(entity│  has_capability(prof=0.85) │  navigation   │
        │ robot)│ ─────────────────────────→ │  patrol       │
        └───────┘                            │  detect       │
                                             └───────────────┘
                                               (capability)
        ┌───────┐  has_capability
        │ dog2  │ ──→ (类似 dog1)
        └───────┘
三、能力推导链
机器人路径（直接注册）：

UE注册 → register_entity() → HNode(entity) + HNode(capability) + HEdge(has_capability)
人类路径（从设备推导）：

UE/配置注册 → register_human_with_devices()
  → HNode(entity) + HNode(device) + HEdge(equips)
  → HNode(channel) + HEdge(provides)
  → EffectiveCapabilityResolver.resolve_and_apply()
    → 读 capability-ontology.yaml 的 enables_rules
    → 匹配可用通道组合
    → 生成 HEdge(enables) + HEdge(has_capability)
设备状态变化时（动态降级/恢复）：

设备离线 → update_device_status()
  → device.attrs.status = "offline"
  → 重新运行 EffectiveCapabilityResolver
  → 移除不再满足的 has_capability 边
  → 例如: glove 离线 → 失去 path_planning, remote_operate
四、超图在任务分配中的使用
任务分配请求
  → Allocator._build_hypergraph(entities, task_plan)
    → 构建任务范围超图 (含 task 节点 + requires 边)
  → _filter_robot_candidates()
    → graph.edges_of(entity_id, "has_capability") 检查能力匹配
  → _filter_human_supervisors()
    → graph.available_channels(entity_id) 检查协作模式
  → _score_robot() / _score_human()
    → compute_robot_utility() / compute_human_utility() 基于超图打分
  → 最终输出: capability_graph = graph.to_dict()
    → 写入 LangGraph 状态
    → 经 Zenoh/WebSocket 广播到前端
    → node-panel-widget "超图关系" 面板展示
五、配置文件
capability-ontology.yaml — 能力本体（taxonomy、channels、enables_rules、协作模式、BT 模式模板、效用权重）
human-profiles.yaml — 操作员设备配置（operator-01 带 ring + XR 眼镜 + 手套，commander-01 带 XR + 平板）
六、关键特性
内存数据结构，不持久化到文件，每次启动从实体注册消息重建
序列化通过 to_dict() / from_dict()，以 JSON 形式在 Zenoh 上传输
动态响应设备状态变化，自动增删能力边
子图提取：subgraph() 可截取任务相关的子图
Protobuf 定义在 protocol/proto/sub/capability.proto，但与 Python 实现有差异（Proto 版更粗粒度）