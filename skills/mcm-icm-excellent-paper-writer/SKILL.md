---
name: mcm-icm-excellent-paper-writer
description: 面向美国大学生数学建模竞赛 MCM/ICM 的英文论文规划、建模、求解、验证与写作技能。适用于多任务赛题、跨学科数据分析、评价模型、动力学模型、网络模型、优化模型、情景分析、政策建议和利益相关者 Memo。强调 Summary Sheet 的一页自洽表达，以及“任务要求—现实机制—数学模型—算法求解—量化结果—验证—行动建议”的完整闭环。
---

# MCM/ICM 优秀论文写作 Skill

## 1. 核心目标

你的任务不是把若干算法拼成一篇英文报告，而是生成一篇让评委能够快速回答以下问题的竞赛论文：

1. 团队究竟如何理解每个 task？
2. 为什么选择这些模型，而不是其他模型？
3. 数学结构如何对应现实机制？
4. 数据、参数和假设从哪里来？
5. 模型输出是否直接回答题目？
6. 结果是否经过验证、扰动或情景检验？
7. 结论能否转化为清晰、可执行的建议？

整篇论文必须形成闭环：

> Problem Tasks → Conceptual Framework → Assumptions and Data → Mathematical Models → Algorithms → Quantitative Results → Validation and Sensitivity → Recommendations or Memo

任何公式都必须对应一个现实关系；任何图表都必须支持一个结论；任何政策建议都必须能够追溯到模型结果。

## 2. 适用范围

本 Skill 适用于 MCM 和 ICM，尤其适合以下题型：

- 多任务、后续任务依赖前一任务结果；
- 评价指标体系与综合评分；
- 动力学、微分方程和系统演化；
- 网络构建、传播、路径、节点打击和监测布局；
- 连续或组合优化；
- 预测、回归、机器学习和概率模型；
- 情景分析、政策设计、项目规划；
- 要求额外提交 Memo、Letter、Executive Report 或其他面向客户的文档。

MCM 通常更重视数学机制、推导和算法；ICM 通常更重视跨学科定义、全球数据、利益相关者、政策落地和沟通。两者都必须保持数学严谨和任务闭环。

## 3. 开始写作前必须获得的信息

尽量获得：

1. 完整赛题、所有 tasks 和附加 deliverables；
2. 当年官方格式、匿名要求、页数限制和 AI 使用披露要求；
3. 附件数据、外部数据源、数据许可和时间范围；
4. 已完成的代码、计算结果、图表和参数；
5. 团队已经决定采用的方法及备选方法；
6. 需要面向的客户、机构、政府或其他利益相关者。

不得虚构数据、引用、训练指标、最优解、概率或敏感性结果。必须区分：

- problem-provided data；
- externally sourced data；
- assumed parameters；
- estimated parameters；
- model-generated results；
- values still pending computation。

缺少计算结果时，用明确占位符，如 `[RESULT_PENDING]`，不要生成看似精确的数字。

## 4. MCM/ICM 论文与国赛论文的关键差异

### 4.1 Summary Sheet 是评委入口

第一页不是普通摘要，而是整篇论文的压缩版本。它应当在脱离正文时仍能说明：

- 问题目标；
- 每个 task 的方法；
- 每个 task 的关键定量结果；
- 验证或敏感性结论；
- 最终建议。

不要把 Summary 写成背景介绍或目录。

### 4.2 英文论文强调叙事和模型命名

模型标题应直接说明用途，例如：

- Global Equity Assessment Model
- Population Competition Model
- Data-driven Forecasting Model
- Network-based Intervention Model
- Probabilistic Project Compliance Model

模型名不是装饰，而是帮助评委建立全文地图。

### 4.3 文献和现实证据进入建模链

MCM/ICM 尤其是 ICM 中，文献不仅用于背景，还应支持：

- 概念定义；
- 指标选择；
- 参数范围；
- 模型机制；
- 政策可行性；
- 结果的外部合理性。

### 4.4 经常需要面向利益相关者输出

Memo、Letter 或政策方案不是摘要的改写。它应减少技术细节，强调：

- 对方为什么应当行动；
- 应当采取什么行动；
- 需要什么资源和权限；
- 分阶段如何实施；
- 预期效果和主要风险。

## 5. 总体工作流

正式写作前，为每个 task 建立任务卡：

| 字段 | 内容 |
|---|---|
| Task | 题目要求的原始目标 |
| Decision question | 评委最终需要看到的结论 |
| Output | 排名、参数、曲线、路径、概率、政策等 |
| Mechanism | 现实中的因果或约束关系 |
| Model | 评价、动力学、网络、优化、预测、概率等 |
| Data | 数据源、时间、样本量、单位 |
| Solver | 解析、数值积分、回归、启发式算法等 |
| Validation | 历史、区域、案例、基线、扰动、交叉验证等 |
| Dependency | 从前一 task 继承的变量或结果 |

每个 task 使用以下闭环：

> State the task → Explain the mechanism → Define variables → Build equations → Estimate parameters → Solve → Present results → Validate → Answer the task

## 6. 推荐全文结构

根据题目调整，但优先采用：

1. **Summary Sheet**
   - Title
   - Summary
   - Keywords
2. **Table of Contents**
3. **Introduction**
   - 3.1 Problem Background
   - 3.2 Restatement of the Problem
   - 3.3 Literature Review
   - 3.4 Our Work / Contributions
   - 3.5 Overall Workflow Figure
4. **Assumptions and Justifications**
5. **Notations**
6. **Data Collection and Preprocessing**
7. **Task 1 / Model I**
   - Modeling rationale
   - Model formulation
   - Parameter estimation or weighting
   - Solution
   - Results and interpretation
   - Validation
8. **Task 2 / Model II**
9. **Subsequent Task Models**
10. **Scenario, Policy or Implementation Analysis**
11. **Sensitivity, Robustness and Uncertainty Analysis**
12. **Model Evaluation**
   - Strengths
   - Limitations
13. **Required Memo / Letter / Report**
14. **References**
15. **Appendix**
16. **Report on Use of AI**, when required by the current rules

不要机械地添加所有章节。若题目只有三个 tasks，可合并章节；若每个 task 采用不同模型，则以 task 或 model 为一级标题更清晰。

## 7. 标题

标题应表达“研究对象 + 核心方案或主张”，通常 8–16 个英文单词即可。

推荐：

- `Network Collaboration: A Data-driven Strategy for Reducing Illegal Wildlife Trade`
- `Asteroid Mining and Global Equity: An Integrated Assessment and Policy Model`
- `Optimizing ... through ...`
- `A Data-driven Framework for ...`

避免：

- `Solution to Problem F`；
- 罗列所有算法；
- 过度文学化且不能说明主题；
- 宣传口号式标题。

## 8. Summary Sheet 写法

### 8.1 长度和结构

Summary 必须放在一页内。作为写作目标，可控制在约 450–650 个英文单词，但最终以当年官方模板和排版是否完整为准。

使用以下顺序：

1. **Opening paragraph：2–4 句**
   - 现实问题；
   - 总体目标；
   - 核心框架或模型组合。
2. **Task-by-task paragraphs**
   - 每个 task 一段；
   - 说明 model、data/method、result、implication。
3. **Validation paragraph**
   - 说明模型验证、敏感性、鲁棒性或不确定性结果。
4. **Closing sentence**
   - 总结最终建议或交付物。
5. **Keywords：3–5 个**

### 8.2 每个 task 的摘要句式

> For Task 1, we first define/identify ... and construct a ... model based on .... Using data from ..., we obtain .... The results indicate that ....

> For Task 2, building on Task 1, we introduce ... and formulate .... We solve the model using ..., yielding ....

> For Task 3, we vary ... to examine .... The scenario results show that ....

### 8.3 Summary 必须出现的数字

优先放入：

- 最终选择、排序或评分；
- 最优参数、目标函数值或路径；
- 预测指标和拟合/测试指标；
- before-versus-after 变化；
- 项目成功概率；
- 敏感性变化幅度。

不要用“significantly improved”代替数字。正文没有的数字不得写入 Summary。

### 8.4 Summary 禁忌

- 超过一半篇幅介绍背景；
- 只写模型名，不写任务对象；
- 只写过程，不写结果；
- 塞入公式或复杂符号；
- 写图号但不给出图所证明的结论；
- 使用 `we successfully solve all problems` 等空话；
- Summary 与正文的数值或模型名称不一致。

## 9. Introduction

### 9.1 Problem Background

控制在 1–3 段，只写形成建模动机所需的事实：

- 问题规模；
- 造成的影响；
- 为什么需要决策；
- 模型服务于谁。

外部事实应引用可靠来源。不要用无引用的宏大叙事填充篇幅。

### 9.2 Restatement of the Problem

按 Task 1、Task 2 等逐项重述，每项明确：

- action：identify、assess、predict、optimize、design、evaluate；
- object；
- output；
- relation with previous tasks。

不得照抄题面长段落，也不能改变题意。

### 9.3 Literature Review

文献综述应短而有结构，通常围绕 2–4 条研究线：

1. 现有方法解决了什么；
2. 哪些数据或机制已有依据；
3. 现有研究缺少什么；
4. 本文如何补足。

推荐结尾：

> Existing studies address A and B separately, but few provide an integrated framework connecting A, B, and the required decision. We therefore develop ...

不要罗列论文标题，不要让综述与后文模型脱节。

### 9.4 Our Work

使用 3–6 个编号条目，每条包含：

> action + named model + data/method + output

随后放一张总体流程图。流程图必须显示 tasks 之间的输入输出关系，而不是仅列出算法名称。

## 10. Assumptions and Justifications

每条假设采用：

> **Assumption N:** ...  
> **Justification:** ...

高质量假设应说明：

1. 假设内容；
2. 现实依据；
3. 简化了哪个环节；
4. 可能造成的偏差；
5. 是否会在敏感性分析中检验。

优先写：

- 市场结构、行为理性和竞争关系；
- 系统稳定性和时间尺度；
- 网络节点、边和信息传播规则；
- 数据代表性和缺失机制；
- 参数是否保持不变；
- 资源、预算、技术或政策可行性。

避免无意义假设，如“all calculations are correct”。对数据来源可靠性的说明不能替代数据质量检验。

## 11. Notations

### 11.1 符号表位置

通常放在 assumptions 之后、data/model 之前。只列核心、跨章节或容易混淆的符号。

建议列：

| Symbol | Description | Unit / Range |
|---|---|---|
| $x_i$ | ... | ... |
| $w_j$ | ... | $[0,1]$ |
| $G=(V,E)$ | ... | - |

### 11.2 符号规范

- 标量斜体，小写向量粗体，小写或大写矩阵粗体；
- 集合用大写字母；
- 下标含义固定；
- 概率、比例和权重注明范围；
- 物理量必须给单位；
- 缩写首次出现先写全称；
- 同一符号不能在不同模型中表示不同含义，除非明确重定义。

符号表不能代替正文解释。变量第一次进入公式前仍要说明现实意义。

## 12. Data Collection and Preprocessing

MCM/ICM 论文应把数据处理作为独立可复现环节，而不是一句“we normalized the data”。

### 12.1 Data Collection

至少说明：

- 数据源和访问对象；
- 时间范围；
- 空间范围；
- 样本数量；
- 指标定义和单位；
- 缺失值和异常值；
- 数据如何服务每个 task。

优先使用国际组织、政府、学术数据库和题目附件。网页来源在参考文献中给出机构名和地址，必要时记录访问日期。

### 12.2 Data Preprocessing

根据需要说明：

1. missing-value treatment；
2. outlier treatment；
3. benefit/cost direction；
4. normalization or standardization；
5. temporal alignment；
6. geographic matching；
7. dimensionality reduction or feature selection；
8. train/validation/test split；
9. uncertainty introduced by preprocessing。

每个处理步骤写出目的和公式。不能只给公式，不解释为什么采用该变换。

### 12.3 数据泄漏检查

预测模型必须检查：

- 未来信息是否进入训练特征；
- 同一实体的相邻时间样本是否被随机拆散导致泄漏；
- 标准化参数是否只由训练集估计；
- 测试集是否参与调参。

## 13. 模型章节的统一写法

每个模型章节按以下顺序组织：

### 13.1 Modeling Objective

用 1 段说明：

- 该模型回答哪个 task；
- 输入是什么；
- 输出是什么；
- 为什么该模型适合。

### 13.2 Real-world Mechanism

先写现实机制，再写公式。说明：

- 哪些主体发生作用；
- 哪些变量相互影响；
- 哪些关系是因果、约束或经验相关；
- 模型中的抽象对象如何映射到现实。

例如，使用 Lotka–Volterra 模型时，必须解释谁是 predator、谁是 prey、每个增长率和作用系数在现实中代表什么，而不是直接复制微分方程。

### 13.3 Definitions and Model Formulation

依次给出：

1. decision/state variables；
2. parameters；
3. objective function or governing equations；
4. constraints；
5. initial/boundary conditions；
6. domain and feasibility conditions。

### 13.4 Parameter Estimation

说明每个参数属于：

- directly observed；
- literature-derived；
- estimated from data；
- calibrated；
- assumed for scenarios。

给出估计方法、样本范围和误差指标。不要把任意设定伪装成估计结果。

### 13.5 Solution Method

说明：

- 为什么选择该求解器；
- 输入、输出和停止条件；
- 关键超参数；
- 随机算法的随机种子或重复次数；
- 计算复杂度或规模；
- 如何检查可行性和收敛。

### 13.6 Results and Interpretation

按以下顺序：

1. 先给最关键的数字或方案；
2. 再展示图表；
3. 解释图表所说明的机制；
4. 最后直接回答 task。

结果段落不能止于 `Figure X shows ...`。必须说清楚为什么该变化支持结论。

### 13.7 Validation

每个核心模型至少使用一种验证；整篇论文至少使用两类互补验证。

## 14. 常见模型类型的写作模板

### 14.1 综合评价模型

推荐顺序：

1. 定义抽象概念；
2. 引入理论框架或评价逻辑；
3. 建立一级、二级、三级指标体系；
4. 解释每个指标为何代表目标概念；
5. 区分 benefit 和 cost indicators；
6. 数据标准化；
7. 权重方法；
8. 综合得分；
9. 排名或分组；
10. 区域、历史或外部指标验证；
11. 权重扰动分析。

权重方法不能只有名称。若使用 AHP，必须给一致性检验；若使用 entropy weight，必须解释信息差异与权重的关系；若混合主客观权重，必须解释组合系数。

### 14.2 动力学或微分方程模型

推荐顺序：

1. 系统主体和相互作用；
2. 状态变量；
3. 每一项的现实来源；
4. governing equations；
5. initial conditions；
6. 参数估计；
7. equilibrium/stability 或数值求解；
8. 时间曲线；
9. 与历史趋势或常识比较；
10. 参数扰动。

每个正负号必须有现实解释。

### 14.3 网络模型

必须定义：

- node；
- directed/undirected edge；
- edge weight；
- time dependence；
- missing edge；
- centrality 或 node importance；
- 网络上的决策任务。

网络图不能只用于展示。后续策略必须使用网络结构，例如：

- 节点优先级；
- 路径优化；
- 监测点覆盖；
- 传播效率；
- 移除节点前后连通性或交易量变化。

### 14.4 优化模型

明确分开：

- decision variables；
- objective；
- hard constraints；
- soft constraints/penalties；
- feasible solution construction；
- algorithm；
- baseline；
- optimality or convergence evidence。

启发式算法必须报告：

- 初始解；
- 邻域操作；
- 接受准则；
- 温度/种群/迭代参数；
- 多次运行结果；
- 最优值与均值或方差。

不要把一次随机运行称为全局最优。

### 14.5 机器学习或预测模型

至少说明：

1. prediction target；
2. features；
3. dataset size；
4. split strategy；
5. architecture/model configuration；
6. training procedure；
7. baseline；
8. evaluation metrics；
9. validation/test results；
10. uncertainty and extrapolation risk。

不要只用训练集相关系数证明模型有效。测试集指标和时间外验证比漂亮的训练曲线更重要。

### 14.6 概率和项目达成模型

推荐顺序：

1. 定义“项目达标”的事件；
2. 将总体目标拆成子策略事件；
3. 说明事件之间是否独立；
4. 选择 Binomial、Poisson 或其他分布的理由；
5. 估计分布参数；
6. 计算单策略和总体概率；
7. 改变突发因素，重新计算概率；
8. 识别最危险因素并给出应对建议。

若事件不独立，不能直接相乘，应使用条件概率、Copula、贝叶斯网络、蒙特卡洛或场景上下界。

## 15. 数学公式写法

### 15.1 公式前后必须有文字

标准结构：

> Based on ..., we define ... as ...
>
> \[ equation \]
>
> where ... . A larger value indicates ... .

不能连续堆放多行公式而不解释。

### 15.2 编号

使用统一编号 `(1), (2), ...`，或按章节 `(3.1), (3.2)`。引用时写 `Equation (10)`，不要写 `the formula above`。

### 15.3 推导深度

保留能够证明以下事项的推导：

- 公式来自什么机制；
- 目标函数如何构造；
- 关键解析结论如何得到；
- 约束为何成立；
- 算法输入为何与模型一致。

省略纯机械代数，将冗长展开或代码放入 Appendix。

### 15.4 量纲与边界

检查：

- 加减项单位一致；
- 指数、对数和三角函数输入无量纲；
- 概率与权重在合法范围；
- 距离、角度、经纬度和时间单位统一；
- 分母不为零；
- 参数极端值下模型仍有定义。

## 16. 图表和视觉表达

优先使用能够减少评委阅读成本的图：

- overall workflow；
- indicator hierarchy；
- heat map or map；
- network graph；
- before/after comparison；
- scenario curves；
- sensitivity curves；
- predicted versus observed；
- path or facility-location map。

每张图必须满足：

1. caption 可独立理解；
2. 坐标轴、单位、图例齐全；
3. 正文先引用后出现；
4. 图中配色和线型在灰度下仍可区分；
5. 字号在整页缩放后可读；
6. 不用 3D、爆炸饼图或装饰色制造视觉噪声；
7. 不重复展示表格已经清楚表达的数据。

表格用于精确比较，图用于趋势和结构。关键数值优先用表格，变化规律优先用图。

## 17. 验证、敏感性和不确定性

### 17.1 可选验证方式

- **Historical validation:** 不同年份结果与已知历史趋势一致；
- **Regional validation:** 空间分布与现实差异一致；
- **Case study:** 将模型应用到具体对象；
- **External validation:** 与公开指标、文献结论或独立数据比较；
- **Train/validation/test:** 数据模型的样本外检验；
- **Baseline comparison:** 与简单策略或现有方法比较；
- **Before/after analysis:** 有策略与无策略对照；
- **Conservation/feasibility check:** 守恒、约束和范围检查；
- **Repeated stochastic runs:** 随机算法的稳定性。

### 17.2 敏感性分析

敏感性分析不是随意改变几个参数。应：

1. 选取不确定且重要的参数；
2. 说明扰动范围；
3. 单因素或全局扰动；
4. 重新求解模型；
5. 计算输出相对变化；
6. 给出曲线、弹性或敏感度排序；
7. 解释何时结论会改变。

可采用：

- Gaussian noise；
- ±5%、±10%、±20% parameter perturbation；
- Monte Carlo simulation；
- Latin hypercube；
- scenario analysis；
- Sobol indices。

### 17.3 不确定性表达

预测和概率结果应给：

- prediction interval；
- confidence interval；
- scenario range；
- empirical distribution；
- worst/base/best cases。

避免把远期预测写成确定事实。

## 18. 情景分析与任务衔接

后续 task 往往要求改变条件。必须明确：

> We retain ... from Model I, modify ..., and recompute ....

情景选择应对应决策变量或现实不确定因素，例如：

- throughput；
- number of participating countries；
- budget；
- compliance rate；
- weather delay；
- organizational resistance；
- network coverage radius。

每个情景至少报告：

- changed input；
- unchanged assumptions；
- resulting output；
- difference from baseline；
- decision implication。

## 19. Policy Recommendations

政策章节必须由模型结果驱动。使用以下结构：

1. **Finding:** 模型发现了什么问题；
2. **Mechanism:** 为什么会发生；
3. **Action:** 谁应采取什么措施；
4. **Implementation:** 需要什么资源、规则或时间；
5. **Expected effect:** 哪个模型指标会改善；
6. **Risk:** 可能的副作用和监测方式。

可将建议分为：

- restriction；
- incentive；
- supervision；
- information sharing；
- international cooperation；
- phased implementation。

避免与模型无关的常识性口号。

## 20. Memo / Letter / Stakeholder Report

### 20.1 标准头部

```text
Date: ...
To: ...
From: Team # ...
Subject: ...
```

### 20.2 正文结构

1. **Purpose:** 为什么写给该对象；
2. **Key finding:** 最重要的模型结论；
3. **Recommended plan:** 2–4 个具体行动；
4. **Timeline:** 分年或分阶段实施；
5. **Resources and authority:** 所需人力、预算、法律权限和合作方；
6. **Expected outcomes:** 可量化目标；
7. **Risks and contingencies:** 主要风险及应对；
8. **Closing:** 明确请求对方采用、批准或合作。

### 20.3 Memo 写作要求

- 面向决策者，不写复杂推导；
- 结论和数字必须来自正文；
- 采用主动语态和行动动词；
- 说明优先级；
- 计划必须可执行，而不是学术总结；
- 通常控制在 1 页，除非题目另有要求。

## 21. Model Evaluation

### 21.1 Strengths

每项优点必须与证据相连：

- Comprehensive：覆盖哪些机制或指标；
- Objective：如何减少主观权重；
- Interpretable：参数如何映射现实；
- Robust：哪项敏感性结果支持；
- Generalizable：如何迁移到其他国家、年份或场景；
- Actionable：如何产生路径、资源或政策决策。

### 21.2 Limitations

高质量限制应包括：

1. 哪个假设或数据造成限制；
2. 可能将结果向哪个方向偏移；
3. 影响哪个 task；
4. 如何改进。

避免写“模型仍可进一步优化”这类空话。

## 22. References

- 正文使用统一编号引用，如 `[1]`；
- 文献、数据、法律、模型来源和网页都应引用；
- 不得编造作者、标题或 DOI；
- 文献表格式保持一致；
- 网页尽量写机构名和页面名，而不是只放裸 URL；
- 关键数据来源应在正文对应位置引用；
- AI 生成的文献线索必须人工核验原始来源。

## 23. Appendix

适合放：

- 完整算法伪代码；
- 代码关键部分；
- 过长的路线、参数和数据表；
- 额外图；
- 补充推导；
- 数据字典。

正文必须独立完成论证。不能把决定模型可信度的关键公式、关键参数或唯一结果藏入 Appendix。

## 24. Report on Use of AI

当年规则要求披露时：

- 写明工具、版本或访问方式；
- 列出实际用途；
- 按要求记录 queries/prompts 和 outputs；
- 说明团队如何核验；
- 不得让 AI 生成的引用未经检查进入正文；
- 不得隐瞒使用情况。

具体格式、是否计入页数和提交位置，以当年官方规则为准。

## 25. 英文写作规范

### 25.1 风格

优先使用短句、主动语态和明确主语：

- `We construct ...`；
- `The results indicate ...`；
- `An increase in x leads to ...`；
- `This finding supports ...`。

避免：

- 连续使用 `Obviously`, `It is easy to see`, `As everyone knows`；
- 夸张表达；
- 口语；
- 无主语的长句；
- 一个句子塞入多个逻辑层次。

### 25.2 术语一致性

建立术语表，全文统一：

- task 名称；
- model 名称；
- indicator 名称；
- baseline/scenario 名称；
- stakeholder 名称；
- 英式或美式拼写。

### 25.3 常见错误检查

- 单复数；
- 冠词；
- 时态；
- 数字与单位间空格；
- Figure/Table/Equation 大小写；
- 逗号后空格；
- 交叉引用；
- 重复句；
- Summary 与正文数值不一致。

## 26. 不能盲目模仿优秀论文的地方

优秀论文的框架值得学习，但执行时必须纠正常见缺陷：

1. 不用训练集高 $R$ 值代替样本外验证；
2. 不把相关关系写成因果关系；
3. 不用一个案例证明模型普遍正确；
4. 不使用没有一致性检验的 AHP；
5. 不把一次启发式运行称为全局最优；
6. 不使用重复句、错误图号或不一致指标；
7. 不让图表好看程度超过信息价值；
8. 不在没有数据支持时给出精确政策效果；
9. 不用“data are accurate”作为唯一数据质量论证；
10. 不把多个模型并列堆砌而缺少变量传递关系。

## 27. 页数和篇幅分配

不得硬编码历年页数限制，先读取当年规则。一般可按正文比例分配：

- Summary Sheet：1 页；
- Introduction + Assumptions + Notations + Data：15%–20%；
- Core models and results：55%–70%；
- Validation + sensitivity + evaluation：10%–15%；
- Memo / additional deliverable：按题目要求；
- References and Appendix：控制在必要范围。

篇幅不足时，优先保留：

1. 关键模型定义；
2. 关键结果；
3. 验证；
4. task 的直接回答。

优先删除背景套话、机械代数、重复图表和非关键代码。

## 28. 生成论文时的执行步骤

### Step 1：解析题目

输出 task dependency graph，说明每个 task 的输入、输出和依赖。

### Step 2：设计总框架

选择少量互补模型，明确变量如何从一个模型传递到下一个模型。

### Step 3：建立数据清单

为每个变量记录来源、单位、时间和处理方式。

### Step 4：完成模型卡

每个模型填写：目的、机制、公式、参数、算法、结果、验证、限制。

### Step 5：先写模型和结果

先完成最难的数学部分，再写 Introduction 和 Summary。

### Step 6：生成图表

只生成用于说明结构、趋势、比较、路径或敏感性的图表。

### Step 7：写 Summary Sheet

从正文反向提取每个 task 的最终结果，不凭记忆重写。

### Step 8：写 Memo 或政策部分

把数学输出翻译为利益相关者能采取的行动。

### Step 9：执行一致性检查

逐项核对标题、符号、公式、图号、表号、数值、引用、任务回答和页数。

## 29. 最终输出要求

生成论文或论文大纲时，应至少输出：

1. 推荐标题；
2. 一页 Summary Sheet 草稿；
3. task dependency graph；
4. 完整目录；
5. assumptions and notations；
6. 每个模型的现实机制、公式、参数和算法；
7. 结果图表清单；
8. validation and sensitivity plan；
9. policy/Memo outline；
10. references and appendix plan；
11. 未完成数据和计算的占位清单；
12. 最终检查报告。

## 30. 最终判断标准

一篇优秀的 MCM/ICM 论文应使评委在短时间内看清：

- Summary Sheet 已经给出主要答案；
- Introduction 解释了问题和贡献；
- 每个模型有现实机制，不是算法堆叠；
- 每个 task 有独立闭环，并与前后任务连接；
- 数据和参数可追溯；
- 公式、算法和图表可复核；
- 结果是量化的；
- 验证和敏感性足以支撑结论；
- 政策和 Memo 由模型结果驱动；
- 英文简洁、结构明确、格式一致；
- 所有核心结论都能从数据和模型中追溯得到。
