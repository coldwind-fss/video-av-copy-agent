# 产品开发基线：视频分析 POC

- 版本：v0.3 实施基线，2026-09-23；既有视觉流程仍为 prompt v0.2 / schema 1.1。
- 决策方：用户及业务组。
- 当前状态：视觉 A/B 和 v0.3 口播 / 文案工作台均已实施；已有 A/B 结果可一键桥接到文案分析；POC 主视图收敛为文本、结构、核心标签和前三秒，逐帧证据默认折叠，A/B 作为次级调试视图；工程验收通过并更新只读分享部署。真实 ASR Provider 已接入部署为阿里云百炼 `qwen-audio-3.1-asr-flash-filetrans`，C10/C20/C30 已消费 ASR 结果完成三条工程回归；实施计划见 [真实 ASR 接入计划](content-analysis-v0.3/ASR-INTEGRATION-PLAN.md)。
- 当前里程碑：完成音轨探测 / 提取、受控 WAV 媒体路由、阿里云异步 ASR、SRT/JSON/纯文本导入、独立 C10/C20/C30 分析、来源及证据验证、历史评价导出与只读部署。
- 当前边界：真实 ASR 和音画工作流工程链路已验证，但没有 ASR 准确率、真实 DeepSeek 音画融合语义或业务质量结论；公网分享继续只读，真实任务在服务器回环管理实例执行。
- 当前业务目标：批量理解素材时优先获得文本、文案结构、标签和前三秒表达方式；原视觉 A/B 实验与真实历史结果保留。

## v0.3 实施入口

新增工作流以 [文案分析 SPEC](content-analysis-v0.3/SPEC.md)、[运行提示词](content-analysis-v0.3/PROMPTS.md)、[实施计划](content-analysis-v0.3/IMPLEMENTATION_PLAN.md)、[验收标准](content-analysis-v0.3/ACCEPTANCE.md) 和 [新字段契约](schemas/content-analysis.schema.json) 为准。工程证据见 [实施验收记录](evidence/content-implementation-acceptance-2026-09-23.json) 与 [本轮显示与桥接证据](evidence/content-display-integration-2026-09-24.json)。旧文档继续约束既有视觉流程；本基线区分两者状态。

## 状态用语

- **Confirmed**：用户明确指定的目标与约束，或已经检查到的事实。
- **Proposed**：本文建议的默认设计，用于继续实施；不伪装成用户已确认。
- **Open**：仍需实际样本、业务判断或接入验证解决的问题。
- **Out of scope**：本版建议暂不覆盖的能力。
- **Implemented / Verified**：分别表示已实现 / 已有验证证据；设计完整不等于已实现。

## 1. 需求分析

**Confirmed**

- 项目处于探索阶段，目标是方案验证和会议讨论，不要求达到生产成品质量。
- 一个轻量工作台内同时支持方案 A 和方案 B。
- A：每秒抽帧，各帧独立分析，再把逐帧结果汇总。
- B：同一批图片按时间顺序联合输入模型，再归纳视频特征。
- 模型使用用户指定的 DeepSeek Vision Flash；已授权使用提供的凭据。
- 核心工作是 AI 工作流、提示词、字段要求与效果展示；前后端保持简单。
- 业务最终希望保留参考视频的可复用表达形式，并替换为自己的品牌主题。
- 结果文字必须有足够对比度；A 保持逐帧卡片，B 的主视图应按连续时间片段呈现，帧详情作为证据展开。
- 业务反馈新增目标：口播 / 切片文本提炼、文案结构分析、标签分类与前三秒分析；纯展示和 3D 素材先不深拆。
- 用户要求接入音频接口及后续分析能力；首个真实 ASR 使用阿里云百炼 `qwen-audio-3.1-asr-flash-filetrans`，已按 SPEC 实施、验收并部署。

**Proposed**

- 本版交付逐帧结果、时序结果、总结、参考视频提示词及可选品牌改编提示词；暂不生成新视频。
- 输入以短商品 / 品牌视频为主；首轮建议 5 条真实样本。
- 比较完整工作流的业务可用性，不将结果解读为单一模型能力的严格因果实验。
- v0.3 建议新增独立文案分析任务，采用 ASR 适配器 + 现有视觉 / 文本模型；允许人工导入文本联调，fixture 必须显式标识。
- 分类字典、人工文本导入、60 秒上限及业务质量数值门槛属于实施建议，详见新 SPEC。

**Open**

- 已有三个视觉测试样本；新增口播链路的代表素材、准确原文 / 时间和业务结构标注仍待补充。
- “可用”的业务阈值尚未确定；不设未经商议的准确率承诺。
- ASR 供应商与账号、词 / 句级时间能力、长于 60 秒的需求、业务标签字典和语调 / 音效范围仍待明确。

## 2. 产品与工作流

**Proposed**：上传一次 → 抽帧一次 → 固定实验配置 → 依次运行 A、B → 在工作台切换查看独立结果 → 人工评审 → 导出报告。

- A：N 次独立单图调用 → 1 次共同汇总调用。
- B：1 次有序多图调用，产出逐帧静态证据、相邻帧证据和完整覆盖帧集的连续片段 → 1 次共同汇总调用。
- 两组使用同一个模型、同一张图片文件、同一套字段和共同汇总模板。
- 汇总节点均仅接收本分支结构化结果；A 不偷偷追加原图，B 不读取 A 的结果。
- A 的主视图按帧展示；B 的主视图按模型分出的连续片段展示，片段内静态帧记录与逐对变化作为可展开证据；片段变化边界只能被相邻采样时刻夹定，界面时间为近似范围，不能视作精确剪辑时间。
- 完整规则见 [POC_SPEC](POC_SPEC.md)。

**v0.3 Implemented**：选择视频 → 音轨探测 / 可选音频提取或人工导入原文 → 文案结构与标签 → 独立的前三秒证据分析 → 报告、历史、评价与导出；已完成 A/B 结果可把画面文字证据桥接为 `manual/subtitle` 输入，结果页先展示全文总结 / 结构，逐帧证据默认折叠。缺 ASR 时有音轨会等待服务；缺时间戳时不假定口播时间；C30 只接收 0–3 秒证据。见 [新工作流](content-analysis-v0.3/SPEC.md)。

## 3. 数据模型

**Proposed**：Video → FrameSet → Comparison → BranchRun(A/B) → Calls / Analysis / Review。

- 一个 FrameSet 固定视频、抽帧参数、时间戳和图片哈希。
- 一个 Comparison 固定模型参数、提示词版本、品牌输入和 FrameSet。
- 一次重跑生成新的尝试记录；不覆盖原模型输出。
- Claim 记录 `value`、`basis`、`evidence_frame_ids`、`note`，允许 `unknown`。
- 数据契约见 [Schema](schemas/analysis.schema.json)；业务交叉校验见 SPEC。
- **v0.3 Implemented**：增加 AudioAsset、TranscriptionJob、不可变 Transcript、ContentAnalysis 与独立 Review；报告保存输入来源、时间粒度、证据和模型调用版本。新 Schema `content-1.0` 与旧视觉 `1.1` 独立演进。

## 4. 架构与工程约定

**Proposed**：Python + FastAPI + 简单 HTML/JS，同源单进程；FFmpeg / FFprobe；直接 HTTP 调用模型；文件持久化。无需专用 Agent 框架、Redis、向量库或前端构建系统。

**Implemented 前端细节**：不使用 React / Vue 等前端框架，也未引入第三方 UI 组件库。FastAPI 用 `HTMLResponse` 提供替换路径前缀后的 HTML；浏览器端使用原生 HTML 表单和语义标签、手写 CSS、原生 JavaScript（Fetch 调 API、DOM 渲染与事件处理）；视频预览使用浏览器原生 `<video>` 控件，图标以少量内联 SVG 实现。没有 npm 构建链。

**Verified**：当前目录最初为空、非 Git 仓库；Python 3.14 和 Node 可用；系统 PATH 没有 FFmpeg，但应用使用 `imageio-ffmpeg` 随包二进制。三个用户提供的 MP4 已完成双分支端到端运行，检查记录见 [功能完整性验证](evidence/functionality-validation-2026-09-23.json)。

- 单用户、本机演示；一次处理一个 Comparison。
- **Current deployment**：另部署了一个只读分享实例至阿里云，通过 Nginx 独立 URL 路径代理到仅监听 loopback 的 systemd 服务；未部署本地 `.env` / 模型凭据，写入请求由代理拒绝。此实例供外部评审浏览已有结果。
- 后台任务状态和结果写入目录，重启将未完成任务标为 interrupted。
- 实现时直接读取环境中的模型配置；文档和导出材料不包含凭据值。
- 模型别名、实际返回模型、参数、输入哈希和模板哈希均记录。
- **v0.3 Implemented / Open**：已增加 FFprobe 优先、随包 FFmpeg 回退的音轨探测与 16 kHz 单声道提取；无 FFprobe 时流偏移保留 unknown。disabled / 显式 fixture Provider、人工导入与内容分析模块已实现；真实供应商 Provider 与其能力验证未接入。继续使用文件存储和单任务准入。

## 5. 最小 API

**Proposed**：视频上传、创建对照任务、读取任务、按帧读取图片、读取原视频、写业务评价、导出报告。具体请求和状态见 SPEC。

- HTTP 202 表示任务已接收，不表示模型分析成功。
- 统一 `error.code / message / retryable`，不建设大型 API 治理体系。
- 页面轮询状态，原生视频控件播放；不引入 WebSocket。
- **v0.3 Implemented**：capabilities、transcriptions、transcripts/import 和 content-analyses API 已实现，详见 [HTTP 契约](content-analysis-v0.3/SPEC.md)。现有错误格式混用 detail / error，新入口兼容读取，新增接口使用明确的 error 对象。

## 6. 实施计划

应用切片状态如下；详见 [实施计划](IMPLEMENTATION_PLAN.md)。

| 切片 | 可观察交付 | 当前状态 |
|---|---|---|
| S0 接入与环境 | 依赖、模型配置、本地服务 | Installed；三条测试素材的完整提示词链路均已实际调用 |
| S1 上传与抽帧 | 视频元信息、共享 FrameSet、帧预览 API | 已用三个 MP4 验证 17、20、17 帧抽取与帧读取 |
| S2 共用模型适配器与 A | 独立帧结果、调用记录、共同汇总 | 三条素材 A 分支均完成，字段与证据通过 Schema 校验 |
| S3 B 与实验约束 | 有序图片联合分析、相邻帧证据、连续片段分组 | v0.2 / schema 1.1 已在 3D.mp4 新任务完成；17 帧归纳为 5 个连续片段，结构与证据校验通过，语义质量待业务评审 |
| S4 工作台与导出 | 高对比度、A/B 独立切换、A 逐帧 / B 分段视图、证据跳转、评价及导出 | 浏览器复核通过：A 显示 17 张逐帧卡；B 显示 5 张片段卡及片段内帧证据；切换不混显两种主视图；页面背景和主要文字对比度已提高 |
| S5 样本验证与演示 | 代表样本结果、错误清单、会议材料 | 三条测试视频的 v0.1 结果均完成；另有一条 3D.mp4 的 v0.2 结果结构验证通过。其余视频尚未用新版重跑，且所有语义质量仍待业务评审 |
| v0.3 P1–P4 | 音轨预留、导入、文案结构 / 标签 / 前三秒、工作台、评价和导出 | 已实施；模型替身工程链路和一次真实 DeepSeek 合成输入烟测通过；详见 [v0.3 evidence](evidence/content-implementation-acceptance-2026-09-23.json) |
| v0.3 P5–P6 | 真实 ASR、标注业务样本、语义评审 | P5.1–P5.3 已部署并完成三条 ASR 回归；音画融合与业务语义质量待视觉模型凭据 / 人工评审 |

## 7. 验证与业务验收

- **Verified**：官方模型列表 HTTP 200；单图与十图请求 HTTP 200；JSON 可解析；帧 ID 顺序一致。证据见 [接入检查](MODEL_ACCESS_CHECK.md)。
- **Verified（设计文件）**：JSON Schema 本身、四份格式示例、四类无效输出反例、示例帧 / 区间关联和本地文档链接已检查。证据见 [设计文件校验](evidence/design-validation-2026-09-23.json)，不代表应用已经通过验证。
- **Verified（测试数据）**：三条视频均 A/B completed；帧覆盖、区间数、Schema、证据引用范围、共享输入哈希、逻辑调用数及报告导出通过核对。证据见 [功能完整性验证](evidence/functionality-validation-2026-09-23.json)。
- 上述三条历史结果使用 `poc-v0.1 / schema 1.0`；不能作为新版连续片段能力的验证。已另生成一条 `poc-v0.2 / schema 1.1` 新任务，旧结果保留并明确标识。
- **Verified（v0.2 新版）**：`3D.mp4` 的新任务 `cmp_68d9bbd84ec04cc1aa91525cdd9f8d23` 完成 17 帧 A/B 分析；B 返回 5 个连续片段，验证脚本检查片段顺序、覆盖、Schema、证据引用和 Markdown/JSON/媒体端点均通过。浏览器中 A 为 17 张逐帧卡、B 为 5 张分段卡；页面计算样式为背景 `rgb(219, 226, 236)`、正文 `rgb(21, 34, 56)`。详见 [v0.2 新版验证](evidence/functionality-validation-v0.2-2026-09-23.json)。
- **Verified（只读分享部署）**：公网页面、健康检查、CSS、JS 均返回 HTTP 200；B 视图显示 5 个连续片段，17/17 帧图加载，原视频可播放；上传/写入入口未展示，写入 API 返回 HTTP 403。服务器端未配置模型密钥，因此分享实例只浏览已完成任务。
- **限制**：B 片段边界是按稀疏采样帧估计的近似范围，不是剪辑点；本轮只用一个视频验证新版分段，结构完整不代表分段或描述语义正确。
- **Not run**：带业务基准标注的语义准确性评分、真人逐帧复核、品牌改编有效性评审及不同采样率的 A/B 对照。结构完整不代表描述一定正确。
- 工程验收检验流程与记录是否正确；业务验收允许结论为“两者均不够好”或“需更多样本”。
- 不将模型自评分数当成真实准确率。
- **v0.3 Verified（工程与只读发布）**：合成媒体覆盖音轨探测 / WAV 提取、无 ASR 的 waiting_provider、无音轨、SRT / JSON / 纯文本校验、模型替身全链路、历史、评价、导出与本机只读写入拦截。另用合成色卡 / 未确认字幕跑过一次真实 `deepseek-flash` 三节点烟测并通过报告 Schema。只读分享版本已部署；6 条 A/B 历史、详情、Markdown 导出和视频 Range 请求可用，公网及应用写入均返回 403。该烟测不能证明 ASR、口播准确性或业务语义质量；分享环境也未配置模型密钥。真实 ASR 与业务质量仍待验收，详见 [新验收标准](content-analysis-v0.3/ACCEPTANCE.md)。
- **v0.3 Verified（真实 ASR 部署与回归）**：云端已接入 `qwen-audio-3.1-asr-flash-filetrans`，公网只读实例报告 ASR `ready`，回环管理实例完成 `3D.mp4`、`口播.mp4`、`切片.mp4` 三条真实回归。口播与切片返回词级时间戳；3D 的供应商无词错误已标准化为 `no_speech`。远端任务号、usage、provider record 写入任务状态；API Key 仅存在 systemd 环境文件。真实 ASR 工程链路通过，但识别质量和音画融合业务验收仍待人工评审。
- **v0.3 Verified（ASR → C10/C20/C30）**：三条真实 ASR 结果已进入视觉、文案结构 / 标签和前三秒节点；口播样本报告为 `completed`，切片因前三秒边界保留 `partial/limited`，3D 因无语音保留视觉证据和 `partial/limited`。一次模型证据别名导致的契约失败已修复并通过重试，所有三份报告均可从公网只读接口读取。工程链路通过，但识别与融合质量仍待业务评审，详见 [音画融合验收记录](evidence/content-asr-fusion-validation-2026-09-24.json)。

## 8. 演示交付

**Current**：本机服务用于持续开发和模型分析；另有阿里云只读分享实例供外部评审查看已完成结果。分享实例以独立 Nginx 路径和 systemd 服务运行，仅监听服务器 loopback，不配置模型凭据，不接受上传、分析、评价或其他写入请求。

- 不以示例 JSON 冒充模型结果。
- 只读分享部署用于临时评审，不代表生产发布或正式服务保障。

## 9. 后续迭代

v0.3 工程工作流已实现；真实 ASR 供应商仍待账号与样本。自动镜头切分、音效 / 语调分析、视频生成、长视频与千条批量吞吐为后续候选，不在本轮实施范围。

## 决策与待验证事项

| ID | 状态 | 内容 |
|---|---|---|
| D01 | Confirmed | 两种分析方法必须同时存在，供业务对照 |
| D02 | Confirmed | 使用指定 DeepSeek 视觉模型，保持工作台轻量 |
| D03 | Confirmed | 当前任务交付实施计划与 SPEC |
| D04 | Confirmed | v0.3 按业务反馈规划文本、文案结构、标签与前三秒，纯展示 / 3D 先不深拆 |
| D05 | Confirmed | 用户授权按该方案实施与部署；首个真实 ASR Provider 为阿里云百炼 `qwen-audio-3.1-asr-flash-filetrans` |
| P01 | Proposed | 两条视觉分支之后复用同一汇总模板 |
| P02 | Proposed | 短视频使用 1 FPS；较长视频自动增大间隔，单视频最多 20 帧 |
| P03 | Proposed | 品牌输入只进入最后的提示词编写，不影响对原视频的识别 |
| Q01 | Open | 真实业务素材及其基准描述 |
| Q02 | Partially confirmed | 口播需求已确认；首个 ASR 供应商 / 模型已部署并返回词级时间戳；三个业务样本的识别质量仍待验收，音乐 / 语调分析仍未纳入 |
| Q03 | Open | 模型在真实素材上的正确性、稳定性和实际耗时 |

## 更新记录

| 日期 | 更新 | 依据 |
|---|---|---|
| 2026-09-23 | 建立 v0.1，固定 A/B 探索目标，附实施规范和最小接入证据 | 本轮用户要求及实际工具结果 |
| 2026-09-23 | 完成本机 FastAPI / 原生 JS 工作台与 A/B 工作流实现；服务启动于 `http://127.0.0.1:8765`；首页与健康端点 HTTP 200 | 本地启动输出及 HTTP 检查；真实业务效果仍待视频样本 |
| 2026-09-23 | 三条测试 MP4 的 A/B 全流程均完成；检查到并修复 FFmpeg 非整数采样率少一帧，以及证据 ID 重复格式问题 | [功能完整性验证](evidence/functionality-validation-2026-09-23.json)；语义准确性待人工评审 |
| 2026-09-23 | 按业务反馈将逐帧、区间和全片结果改为 A/B 独立切换视图，切换复用已完成结果且不触发模型请求 | 本地 Edge 实际打开已完成样本并验证 A/B 切换、字段单显及方案状态 |
| 2026-09-23 | 按本轮反馈提高 UI 对比度，并将 B 的新版输出契约升级到 prompt v0.2 / schema 1.1，显式生成连续片段，主视图改为片段卡片 | [v0.2 新版验证](evidence/functionality-validation-v0.2-2026-09-23.json)：3D 样本输出 5 段并通过结构验证；浏览器确认 A/B 主视图隔离及对比度 |
| 2026-09-23 | 应用户反馈进一步加深页面工作区底色到 `#dbe2ec`，保留白色内容卡片；记录前端实际技术栈 | 浏览器复核计算样式：body 背景 `rgb(219, 226, 236)`、正文 `rgb(21, 34, 56)`、卡片白底；见第 4 节前端实现说明 |
| 2026-09-23 | 按业务反馈新增 v0.3 文案分析规划，覆盖音频预留、人工导入建议、结构标签、前三秒隔离和分层验收；运行时未更改 | 本轮用户明确先规划；[规划入口](content-analysis-v0.3/SPEC.md)，[设计检查](evidence/content-plan-validation-2026-09-23.json) |
| 2026-09-23 | 按后续授权实施 v0.3 P1–P4：音轨探测 / 提取预留、人工文本导入、独立视觉 / 文案 / 标签 / 前三秒节点、结构化报告及工作台；合成验收与真实模型合成输入烟测通过；只读分享发布及历史兼容检查通过。真实 ASR 和业务语义仍未验收 | [实施验收记录](evidence/content-implementation-acceptance-2026-09-23.json) |
| 2026-09-24 | 依据最新业务反馈：增加 `POST /api/comparisons/{comparison_id}/content-analysis`，从既有 A/B 的可见文字证据启动 C10/C20/C30；结果页把全文总结 / 文案结构置于逐帧证据之前，A/B 帧与视觉观察默认折叠；用 3D 样本真实 DeepSeek 跑出一条 `manual/subtitle` 报告并复制到只读分享实例 | [本轮显示与桥接证据](evidence/content-display-integration-2026-09-24.json) |
| 2026-09-24 | 按 POC 验证目标收敛前端：默认进入文案分析实验页，主视图只保留文本提炼、文案结构、核心标签和前三秒；A/B、历史筛选、评分、导出和逐帧证据降为次级 / 折叠内容，后端工作流与 Schema 不变 | [本轮显示与桥接证据](evidence/content-display-integration-2026-09-24.json) |
| 2026-09-24 | 用户提供百炼 API Key 并确认采用 Qwen-Audio 3.1 ASR；官方样例调用完成，返回文本和时间戳。未把凭据写入项目；真实业务视频和端到端 Provider 仍未实施 | [真实 ASR 接入计划](content-analysis-v0.3/ASR-INTEGRATION-PLAN.md) |
| 2026-09-24 | 完成真实 ASR Provider、受控 WAV 路由和云端部署；回环管理实例完成三条视频回归：口播 / 切片为词级转写，3D 为 no_speech；公网分享保持只读 | [真实 ASR 接入计划](content-analysis-v0.3/ASR-INTEGRATION-PLAN.md) |
| 2026-09-24 | 将三条真实 ASR 结果接入 C10/C20/C30；口播报告 completed，切片 / 3D 按时间边界和无语音事实保留 partial；修复模型元数据证据别名导致的契约失败并重跑通过 | [音画融合验收记录](evidence/content-asr-fusion-validation-2026-09-24.json) |
| 2026-09-24 | 对新增 `5-43.78.mp4` 与 `1-47.13%.mp4` 完成素材哈希确认、上传、ASR 和 C10/C20/C30 回归；分别得到 partial/limited 与 completed 报告，公网列表共 5 条 ASR 结果 | [新视频回归记录](evidence/new-video-regression-2026-09-24.json) |
