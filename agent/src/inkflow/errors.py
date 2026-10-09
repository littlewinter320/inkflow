class InkFlowError(Exception):
    """墨流可预期错误的基类。"""

    recovery_node: str = ""
    recovery_attempts: int = 0
    recovery_exhausted: bool = False


class ConfigurationError(InkFlowError):
    """配置不完整或不可用。"""


class ProjectError(InkFlowError):
    """小说项目无效或状态不允许当前操作。"""


class ProjectBusyError(ProjectError):
    """项目写锁由另一项仍在执行的工作占用，可等待条件变化。"""


class ProviderError(InkFlowError):
    """模型供应商调用失败。"""


class ValidationGateError(InkFlowError):
    """确定性门禁拒绝继续。"""
