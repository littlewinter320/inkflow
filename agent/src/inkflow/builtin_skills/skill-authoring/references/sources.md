# 来源与适配记录

核对时间：2026-10-31。

- [Agent Skills规范](https://agentskills.io/specification)：name/description、标准入口、可选资源与按需披露。墨流在agents/inkflow.json补实际角色、步骤和工具适配，不改变标准SKILL.md结构。
- [Claude Agent Skills概览](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview)：复用工作说明与资源的分层读取。其代码执行环境不等于墨流拥有相同沙箱。
- [prompts.chat prompt-lookup](https://github.com/f/prompts.chat/blob/main/plugins/claude/prompts.chat/skills/prompt-lookup/SKILL.md)，Git blob dc04ac1dbf9a72eaad7c88ae764df46e6c13a43e。仅作触发、工具与变量填写的模板参考，未注册其MCP或improve_prompt服务。
- [prompts.chat LICENSE](https://github.com/f/prompts.chat/blob/main/LICENSE)：项目明确区分代码/站内内容MIT与用户提示词数据CC0。这里的插件模板按MIT保留许可，不笼统宣称所有网站内容可任意使用。
- [Ponytail](https://github.com/DietrichGebert/ponytail/blob/main/skills/ponytail/SKILL.md)，Git blob d64edd32265ba21cbf5270158f04e97d96925f27，MIT，2026 DietrichGebert。墨流重新表述为当前小说任务的最小完整工作，不执行其工程宿主规则。

完整许可见assets/THIRD_PARTY_LICENSES.txt。原模板下载到D盘参考区，不把第三方整套应用作为墨流依赖。

