"""与平台组对接的最小边界；虚拟实现仅在本地记录模拟动作。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol


# 动作名称约定：浏览、点赞、评论、发帖、跳过；平台实现和决策解析共用这些值。
ActionKind = Literal["browse", "like", "comment", "post", "skip"]


@dataclass(frozen=True)
class Post:
    """平台向 agent 展示的一条帖子，先保留决策所需的最小信息。"""

    # 平台侧的帖子编号，供点赞或评论时精确指定目标。
    post_id: str

    # 帖子作者的名称或标识，供 agent 理解内容来源。
    author: str

    # agent 可以阅读的帖子正文，由平台组负责获取并转换为文本。
    text: str


@dataclass(frozen=True)
class Observation:
    """一次 observe() 返回的信息快照，只包含此次提供给该账号的可见内容。"""

    # 本次读取使用的实验账号编号，用于把观察结果与后续决策对应起来。
    account_id: str

    # 本次可见的帖子集合；空元组表示此次没有提供帖子，元组避免读取后意外增删。
    posts: tuple[Post, ...]


@dataclass(frozen=True)
class Action:
    """一个待执行动作；结构化决策最终应转换成此对象，再交给平台执行。"""

    # 动作类型，取 ActionKind 中的一个值，例如 like 表示点赞。
    kind: ActionKind

    # 目标帖子编号；点赞和评论必须填写，发帖或跳过等动作可为 None。
    target_id: str | None = None

    # 评论或新帖的正文；评论和发帖必须提供非空文本，其他动作可为 None。
    text: str | None = None


@dataclass(frozen=True)
class ActionResult:
    """平台对一次执行请求的反馈，后续可以把此结果作为下一轮输入交给 agent。"""

    # 执行动作所使用的实验账号编号。
    account_id: str

    # 本次尝试执行的动作，保留目标和正文，便于将请求与反馈对应起来。
    action: Action

    # 平台报告动作是否成功；表示执行结果，不评价 agent 的决策质量。
    success: bool

    # 是否为模拟执行；虚拟平台固定为 True，真实平台实现应返回 False。
    simulated: bool

    # 供研究者或 agent 阅读的执行说明，可描述成功内容或失败原因。
    message: str


class PlatformPort(Protocol):
    """平台组需要实现的两个方法；runner 以外的回合流程可通过此接口接入平台。"""

    def observe(self, account_id: str) -> Observation:
        """读取一次平台信息，返回本次 agent 可见的帖子。"""
        ...

    def execute(self, account_id: str, action: Action) -> ActionResult:
        """以指定账号执行 action，返回执行反馈，并通过 simulated 标明是否为模拟。"""
        ...


class FakePlatform:
    """固定输入、明确的模拟结果，供其他组接入前独立联调。"""

    def __init__(self) -> None:
        # 当前实例执行过的模拟动作结果，按发生顺序保存所有账号的记录；仅在内存中保留。
        self.history: list[ActionResult] = []

    def observe(self, account_id: str) -> Observation:
        """为传入账号提供两条固定帖子；重复读取内容相同，便于联调时复现输入。"""
        return Observation(account_id, (
            Post("demo-001", "虚拟用户甲", "今天想讨论人工智能如何改变日常工作。"),
            Post("demo-002", "虚拟用户乙", "数字平台上的推荐会影响我们看到哪些观点？"),
        ))

    def execute(self, account_id: str, action: Action) -> ActionResult:
        """校验动作必需信息，然后记录模拟成功结果；无效动作抛出 ValueError。"""
        if action.kind not in {"browse", "like", "comment", "post", "skip"}:
            raise ValueError(f"不支持的动作：{action.kind}")
        if action.kind in {"like", "comment"} and not action.target_id:
            raise ValueError("点赞和评论需要目标帖子编号。")
        if action.kind in {"comment", "post"} and not (action.text or "").strip():
            raise ValueError("评论和发帖需要正文。")
        result = ActionResult(account_id, action, True, True, "模拟执行成功，仅记录到虚拟平台。")
        self.history.append(result)
        return result
