"""Data models and event schemas for Antigravity Agent bridge."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


@dataclass
class ModelOption:
    index: int
    id: str
    display_name: str
    badge: str
    description: str
    tier: str
    aliases: List[str] = field(default_factory=list)
    supports_thinking: bool = False
    quota_remaining: Optional[float] = None
    reset_time: Optional[str] = None
    is_recommended: bool = True
    code: str = ""

    def __post_init__(self):
        if not self.code:
            self.code = str(self.index)


AVAILABLE_MODELS: List[ModelOption] = [
    ModelOption(
        index=1,
        code="1.1",
        id="gemini-3.8-flash-high",
        display_name="Gemini 3.8 Flash (High)",
        badge="High / Fast",
        description="新一代多模态旗舰 Flash 模型，高智能、快速响应",
        tier="flash",
        aliases=["1.1", "1", "gemini-3.8", "gemini-3.8-flash", "3.8", "flash-3.8", "flash", "flash-high"],
        supports_thinking=True,
    ),
    ModelOption(
        index=2,
        code="2.1",
        id="gemini-3.7-flash-high",
        display_name="Gemini 3.7 Flash (High)",
        badge="Medium",
        description="经典稳定高效通用模型，综合能力优秀",
        tier="flash",
        aliases=["2.1", "2", "gemini-3.7", "gemini-3.7-flash", "3.7", "flash-3.7"],
        supports_thinking=True,
    ),
    ModelOption(
        index=3,
        code="3.1",
        id="gemini-3.6-flash-high",
        display_name="Gemini 3.6 Flash (High)",
        badge="Medium / Fast",
        description="极速轻量模型，资源开销低，适合常规问答与代码探索",
        tier="flash_lite",
        aliases=["3.1", "3", "gemini-3.6", "gemini-3.6-flash", "3.6", "flash-3.6", "flash_lite", "lite"],
        supports_thinking=True,
    ),
    ModelOption(
        index=4,
        code="4.1",
        id="gemini-pro-agent",
        display_name="Gemini 3.1 Pro (High)",
        badge="Low / Reasoning",
        description="专业级深层推理模型，适合大型架构与高难度逻辑任务",
        tier="pro",
        aliases=["4.1", "4", "gemini-3.1", "gemini-3.1-pro", "3.1", "pro-3.1", "pro", "gemini-pro"],
        supports_thinking=True,
    ),
    ModelOption(
        index=5,
        code="5",
        id="claude-sonnet-4-6",
        display_name="Claude Sonnet 4.6 (Thinking)",
        badge="Thinking",
        description="Anthropic 思考增强模型，超强代码生成与复杂算法推理",
        tier="pro",
        aliases=["5", "5.1", "claude-sonnet", "sonnet", "claude-sonnet-4.6", "sonnet-4.6", "claude-4.6", "sonnet4.6"],
        supports_thinking=True,
    ),
    ModelOption(
        index=6,
        code="6",
        id="claude-opus-4-6-thinking",
        display_name="Claude Opus 4.6 (Thinking)",
        badge="Thinking",
        description="Anthropic 顶级超旗舰模型，处理极度复杂的工程与推理任务",
        tier="pro",
        aliases=["6", "6.1", "claude-opus", "opus", "claude-opus-4.6", "opus-4.6", "opus4.6"],
        supports_thinking=True,
    ),
    ModelOption(
        index=7,
        code="7",
        id="gpt-oss-120b-medium",
        display_name="GPT-OSS 120B (Medium)",
        badge="Medium",
        description="开源大参数模型，兼具高容量与中等推理能力",
        tier="pro",
        aliases=["7", "7.1", "gpt-oss", "gpt-oss-120b", "gpt", "oss", "120b"],
        supports_thinking=True,
    ),
]


def update_available_models(new_models: List[ModelOption]) -> None:
    """Update the global available models list at runtime."""
    global AVAILABLE_MODELS
    if new_models:
        AVAILABLE_MODELS = new_models


def get_model_by_identifier(identifier: str, model_list: Optional[List[ModelOption]] = None) -> Optional[ModelOption]:
    """Resolve a model option by hierarchical code (1.1), numerical index (1), id, alias, or display name."""
    models = model_list if model_list is not None else AVAILABLE_MODELS
    cleaned = identifier.strip().lower()

    # 1. Check exact hierarchical code match (e.g. "1.1", "1.2", "4.1")
    for opt in models:
        if opt.code and opt.code.lower() == cleaned:
            return opt

    # 2. Check numeric major index (e.g. "1" matches the default/first option in group 1, like 1.1)
    if cleaned.isdigit():
        for opt in models:
            if opt.code == f"{cleaned}.1" or opt.code == cleaned:
                return opt
        idx = int(cleaned)
        for opt in models:
            if opt.index == idx:
                return opt

    # 3. Check exact id
    for opt in models:
        if opt.id.lower() == cleaned:
            return opt

    # 4. Check aliases
    for opt in models:
        if any(cleaned == a.lower() for a in opt.aliases):
            return opt

    # 5. Check prefix / substring match
    for opt in models:
        if cleaned in opt.id.lower() or cleaned in opt.display_name.lower():
            return opt

    return None


class ModelTier(str, Enum):
    FLASH_LITE = "flash_lite"
    FLASH = "flash"
    PRO = "pro"

    @classmethod
    def default(cls) -> "ModelTier":
        return cls.FLASH

    @classmethod
    def from_str(cls, val: str) -> "ModelTier":
        opt = get_model_by_identifier(val)
        if opt:
            return cls(opt.tier)
        return cls.FLASH



@dataclass
class AgentEvent:
    step_index: int
    raw_step: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ThinkingEvent(AgentEvent):
    thought: str = ""


@dataclass
class ToolCallEvent(AgentEvent):
    tool_name: str = ""
    tool_summary: str = ""
    tool_action: str = ""
    arguments: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResultEvent(AgentEvent):
    tool_name: str = ""
    output_preview: str = ""
    is_error: bool = False


@dataclass
class ContentEvent(AgentEvent):
    content: str = ""


@dataclass
class TurnCompleteEvent(AgentEvent):
    final_content: str = ""


@dataclass
class ErrorEvent(AgentEvent):
    error_message: str = ""


@dataclass
class ArtifactReviewEvent(AgentEvent):
    artifact_path: str = ""
    artifact_name: str = ""
    summary: str = ""
    request_feedback: bool = True


@dataclass
class ConversationInfo:
    conversation_id: str
    created_at: str
    title: str = ""
    first_prompt: str = ""
    model: str = "default"
    workspace: str = ""


@dataclass
class SessionState:
    chat_id: int
    active_conversation_id: Optional[str] = None
    model: str = "gemini-3.8-flash"
    pending_model_switch: Optional[str] = None
    workspace: str = ""
    batch_mode: bool = False
    batch_buffer: List[str] = field(default_factory=list)

    @property
    def conversation_id(self) -> Optional[str]:
        return self.active_conversation_id

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "active_conversation_id": self.active_conversation_id,
            "model": self.model,
            "pending_model_switch": self.pending_model_switch,
            "workspace": self.workspace,
            "batch_mode": self.batch_mode,
            "batch_buffer": self.batch_buffer,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionState":
        return cls(
            chat_id=data["chat_id"],
            active_conversation_id=data.get("active_conversation_id"),
            model=data.get("model", "gemini-3.8-flash"),
            pending_model_switch=data.get("pending_model_switch"),
            workspace=data.get("workspace", ""),
            batch_mode=data.get("batch_mode", False),
            batch_buffer=data.get("batch_buffer", []),
        )
