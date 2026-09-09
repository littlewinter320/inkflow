class InkFlowError(Exception):
    """墨流可预期错误的基类。"""


class ConfigurationError(InkFlowError):
    """配置不完整或不可用。"""


class ProjectError(InkFlowError):
    """小说项目无效或状态不允许当前操作。"""


class ProviderError(InkFlowError):
    """模型供应商调用失败。"""


class ValidationGateError(InkFlowError):
    """确定性门禁拒绝继续。"""
