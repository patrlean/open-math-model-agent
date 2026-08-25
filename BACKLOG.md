# Backlog

## T2

### PDF 示意图视觉理解

- 当前状态：暂缓。
- 问题：PDF 文字可以直接提取，但 DeepSeek 文本 API 无法理解其中的流程图、结构示意图、拓扑图、图例、箭头与空间关系。
- 计划：将含图页面渲染为图片，使用独立的视觉模型生成结构化图像说明，再把对象、连线、标签、尺寸、图例和关系写入 `problem.md`，主 Agent 继续使用 DeepSeek 完成推理。
- 注意：OCR 只能作为图中文字识别的辅助，不能替代示意图语义理解。

### Append-only Working Memory 与上下文缓存实验

- 当前状态：协议与可切换实验机制已实现，等待同题对照运行与指标分析。协议见 `docs/working-memory-protocol.md`。
- 问题：当前 Working Memory 固定覆盖第二条 system message；当 plan、decisions 或 artifact index 更新时，靠后的完整对话前缀可能失去缓存复用。
- 实验方案：保留稳定的 Working Memory protocol，后续仅在状态真实变化时，于完整 tool result 之后追加带 epoch、版本号和状态哈希的完整 memory snapshot；不再覆盖早期消息。上下文压缩后进入新 epoch 并重新生成完整快照。通过 `context.working_memory_mode` 在 `append_only` 与旧 `replace` 模式之间切换。
- 对照指标：DeepSeek cached/uncached input tokens、请求费用、上下文长度、首 token/完整响应耗时、重复或遗漏既有决策的频率，以及中断恢复正确率。
- 约束：不能把 memory 插入 assistant tool_calls 与对应 tool result 之间；旧快照必须明确由最新版本取代；缓存优化不能牺牲任务连续性。

### Assumptions & Justifications 独立强化实验

- 当前状态：待设计与实验。
- 问题：假设不充分、无依据或与题目条件冲突时，后续模型结构、参数、约束和结论容易整体跑偏；在论文阶段补写假设无法修复已经建立错误的模型。
- 实验方案：在读取题目、数据审计之后、正式建模和任务委派之前，增加独立的 Assumptions & Justifications 阶段。主 Agent 先区分题目事实、必要假设、建模简化和待验证推断，再由独立协作 Agent 对关键假设进行反例、边界条件、量纲、可识别性和结论敏感性审查。
- 建议产物：结构化 `assumptions.json`，每项至少包含 `id`、`statement`、`type`、`justification`、`evidence`、`scope`、`downstream_usage`、`risk_if_false` 和 `validation_plan`；论文中的假设章节由该文件生成或核对。
- 验收目标：关键假设均有理由或证据，未静默改变题目条件，代码与论文使用同一套假设；高风险假设具有敏感性/替代模型检查，并能追踪其影响到的公式、参数、实验和结论。
- 对照实验：在同一组题目上比较现有流程与强化流程的验证退回次数、模型结构性错误数、返工步骤/token、最终论文一致性及人工评分。
