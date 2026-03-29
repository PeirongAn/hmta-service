"""Entity schema — Base + extension polymorphism.

Supports both robot and human entities.
Human entities include wearable devices, interaction channels,
and cognitive profiles for human-machine allocation.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# ── Capability parameter definition ──────────────────────────────────────────

class ParamDef(BaseModel):
    """Defines the calling signature of a single capability parameter.

    UE registers these alongside each structured_capability so that the
    HMTA ParamResolver knows who should supply the value and via what
    interaction mode (auto-fill vs Minimap path-draw vs single-point pick…).
    """
    name: str
    type: str                                      # vec3 | array<vec3> | float | string | bool | int
    required: bool = True
    source: str = "llm"                            # llm | human | runtime | config  (| = priority chain)
    input_mode: str = "auto"                       # auto | point_pick | path_draw | region_select | text_input | select
    default_value: str | None = None
    enum: list[str] | None = None                  # allowed values when type=string
    description: str = ""


class CapabilityEntry(BaseModel):
    """A single structured capability reported by an entity during registration."""
    name: str                                      # canonical capability id, e.g. "navigation.move_to"
    mode: str = "autonomous"                       # autonomous | supervised | remote_control
    proficiency: float = 1.0                       # 0.0 ~ 1.0
    params: dict[str, str] = {}                    # static config metadata (max_speed, range_m …)
    param_defs: list[ParamDef] = []                # function signature (new — optional, backward-compat)
    params_schema: dict[str, Any] = {}             # legacy field kept for compat
    constraints: dict[str, Any] = {}


# ── Human device + channel ────────────────────────────────────────────────────

class DeviceEntry(BaseModel):
    """A wearable device carried by a human operator."""
    device_id: str
    type: str                                      # ring | xr_glasses | glove | headset
    status: str = "online"                         # online | offline | degraded
    channels: list[str] = []                       # interaction channels this device provides
    constraints: dict[str, Any] = {}               # battery, render_fps …


class DeviceStatus(BaseModel):
    """Lightweight device status for high-frequency state messages."""
    device_id: str
    status: str = "online"
    battery: float | None = None                   # 0.0 ~ 1.0


class CognitiveProfile(BaseModel):
    """Static cognitive characteristics of a human operator."""
    max_concurrent_tasks: int = 3
    decision_accuracy: float = 0.8                 # 0.0 ~ 1.0
    avg_response_sec: float = 10.0


# ── Entity base + extensions ──────────────────────────────────────────────────

class EntityBase(BaseModel):
    entity_id: str
    entity_type: Literal["robot", "human"]
    display_name: str = ""
    status: str = "idle"                           # idle / busy / offline
    comm_status: str = "online"                    # online / degraded / offline
    position: dict[str, float] = {}                # {x, y, z}
    capabilities: list[CapabilityEntry] = []
    extensions: dict[str, Any] = {}                # locomotion / sensors / authority_level …


class HumanEntityData(BaseModel):
    """Full human registration payload (mirrors Zenoh zho/entity/registry message)."""
    entity_id: str
    display_name: str = ""
    category: str = "ENTITY_HUMAN"
    role: str = ""                                 # ROLE_OPERATOR | ROLE_COMMANDER | ROLE_OBSERVER
    authority_level: str = "operator"              # observer | operator | commander
    devices: list[DeviceEntry] = []
    cognitive_profile: CognitiveProfile = Field(default_factory=CognitiveProfile)
    metadata: dict[str, Any] = {}


# ── Environment + task context ────────────────────────────────────────────────

class EnvironmentMeta(BaseModel):
    map_id: str = ""
    zones: dict[str, Any] = {}
    obstacles: list[dict[str, Any]] = []
    weather: str = "clear"
    visibility: float = 1.0


class TaskContext(BaseModel):
    task_id: str
    objective: str
    doctrine: str = ""
    constraints: list[str] = []
    priority: str = "normal"
    deadline_sec: float | None = None
