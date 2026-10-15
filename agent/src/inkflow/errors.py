import re


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


def provider_failure_guidance(message: str) -> dict[str, str] | None:
    match = re.search(r"\bHTTP\s+(401|402|403)\b", message, re.I)
    if not match:
        return None
    explanations = {
        "401": ("当前密钥未通过认证", "模型服务没有接受当前密钥（401）。",
                "在模型设置中核对或更新密钥，并检查普通API与Token Plan地址是否配套。"),
        "402": ("生成服务的余额或额度需要处理", "模型服务因账户余额或计费状态拒绝了这次生成请求（402）；能读取模型列表不表示生成额度可用。",
                "核对当前密钥所属账户的可用余额或订阅状态；确认入口与密钥配套后再检查生成连接。"),
        "403": ("模型服务拒绝访问", "模型服务拒绝了这次请求（403）。",
                "核对账号的模型权限及服务商访问限制。"),
    }
    title, explanation, next_action = explanations[match[1]]
    return {"title": title, "message": explanation, "next_action": next_action}
