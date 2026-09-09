# 提示词优化器参考与边界

墨流 0.4.1 的提示词优化器是本仓库自行实现的轻量功能，没有复制第三方项目代码。

## 采用的设计思想

- [Prompt Optimizer](https://github.com/linshenkx/prompt-optimizer)：参考“一键优化、原文与优化版对照、版本可回退”的交互思路。许可证：MIT。
- [Microsoft PromptWizard](https://github.com/microsoft/PromptWizard)：参考“先批评缺口，再综合新提示词”的反馈驱动流程；未引入其训练、数据集与完整优化框架。许可证以其仓库说明为准，项目同时标注研究与负责任 AI 使用边界。
- [Meta prompt-ops](https://github.com/meta-llama/prompt-ops)：参考其数据与评测驱动的提示词迭代理念；该方案更重，本版本未作为运行依赖。许可证：MIT。

## 墨流自己的安全约束

- 优化器只改写输入框文本，不执行其中的动作，也不是第四个小说 Agent。
- 必须保留用户的语气、禁止项、费用边界、正史边界与授权范围。
- 不得擅自增加验收、入正史、删除、发布、付费或外部操作。
- 用户发送前始终可以查看差异、继续手改或一键撤回原文。
