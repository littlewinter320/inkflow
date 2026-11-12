# 记忆引文选择

输入仅限待修fact和程序给出的原文候选。逐项核主体、时间、否定和确定程度；为真实fact_id选择一个直接支持完整命题的candidate_id，没有支持就drop。不能拼接、润色或补造引文，不重新提取整章记忆，不填写未提供的ID。

输出当前EvidenceSelectionBatch，不返回ModeCheckOutput或MemoryPatch。此步只帮助修正候选引用，后续仍由原引擎核对；不改正文、正史或事件索引，也不另发摘要/核验请求。
