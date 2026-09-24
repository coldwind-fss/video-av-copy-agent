# 项目完整上下文说明（迁移阅读入口）

> 本文件用于把本项目交给新的开发者或 Agent 继续维护。它记录项目从最初的视觉视频拆解 POC，到音频转写、文案分析、音画融合、云端只读分享和新增素材回归的完整演进。文中凡涉及密钥、私钥、密码的内容只写配置位置或变量名，不写真实值。
>
> 建议接手顺序：先读本文件，再读 `README.md`、`docs/PRODUCT_BASELINE.md`，然后按需要读 `docs/POC_SPEC.md`、`docs/PROMPTS.md`、`docs/content-analysis-v0.3/` 和 `docs/evidence/`。

## 0. 当前结论（截至 2026-09-24）

项目是一个轻量的视频内容拆解与文案分析 POC，已经完成两条工作流：

1. **视觉 A/B 工作流**
   - A：按时间抽帧，每帧独立调用视觉模型，再汇总。
   - B：把按时间排序的多帧放在同一有序请求中，分析连续画面与变化，再汇总。
   - A 的主视图是逐帧卡片；B 的主视图是连续片段，片段里的帧和变化证据可以展开。
2. **v0.3 文案/音画工作流**
   - C10：画面证据和口播/字幕证据整理。
   - C20：文案结构、痛点、产品介绍、福利/卖点、行动和标签分析。
   - C30：前三秒分析，只使用 0–3 秒范围内的证据。
   - 音频通过阿里云百炼 `qwen-audio-3.1-asr-flash-filetrans` 进行异步 ASR，转写结果保留词/句时间戳，再供 C10/C20/C30 消费。

当前工程链路已跑通并部署：真实 ASR 已完成旧三条素材和新增两条素材的回归；报告 Schema、证据引用和导出端点通过结构校验。业务语义准确率、音画融合质量和品牌可复用性仍需人工评审，不能把工程通过等同于业务效果达标。

当前公网只读分享地址：

`http://121.41.164.222/poc-review-c15a6fdf7ead48fb8505/`

该地址只浏览已完成结果，不能上传、触发模型、评价或写入。真实任务在服务器回环管理实例上执行。

## 1. 项目背景与最初目标

业务组最初希望把短视频输入 AI，归纳出视频主题、场景、运镜、构图、风格、色彩、动作等特征，再据此生成相似风格、替换为品牌主题的创作提示词。早期判断是：单个模型直接理解整段视频的成本和稳定性未知，因此先做一个可验证的抽帧 POC。

最初方案是 10 秒视频每秒抽一张图，对每张图做视觉识别，再把 10 张结果串联总结。参考网站 `https://nz-hot-video.surge.sh/` 的按时间/帧展示启发了工作台形态：输入视频、显示时间节点、显示画面描述和最终总结。

讨论后确定两个对照分支，不能把结果混在一起：

- **方案 A（单帧独立）**：每张图分别识别，主要验证静态信息提取能力。
- **方案 B（有序多帧）**：连续多帧按顺序放入同一个请求，验证模型能否理解相邻画面变化、动作发展和镜头连续性。

两种方式使用同一组采样图片、同一个视觉模型和统一字段，结果页提供路由/切换，但 A 和 B 的主视图分别展示，不把 B 渲染成另一套逐帧 A 卡片。

## 2. 需求演进时间线

### 2.1 视觉 A/B POC（v0.1）

用户明确：这是探索性方案验证，不追求大型应用的前后端设计；核心是模型工作流、提示词编排、字段约束、证据链和效果展示。工作台保持轻量，上传一次视频，内部抽帧，执行 A/B，比较结果，支持 JSON/Markdown 导出和人工评价。

视觉字段逐步固定为：主题/主体、场景、景别、角度、构图、光线、色彩、姿态、画面文字，以及最终全片总结、参考视频提示词和可选品牌改编提示词。未知或证据不足时允许 `unknown`，不强迫模型编造。

### 2.2 A/B 展示调整

业务反馈指出：两种方式不能并排混成一个连续长表；应当每帧明确对应时间，A/B 分开查看。后续又指出 B 不应该只是显示 0 秒、1.5 秒的单帧结果，而应该按连续几帧组成片段，给出“这段发生了什么、动作怎么发展、镜头是否连续”，并保留静态帧作为可展开证据。

UI 经过多轮收敛：

- 页面背景从很浅的灰白加深到 `#dbe2ec`，正文使用深色 `#152238`，白色卡片承载内容，提高对比度。
- 前端不是 React/Vue，也没有 UI 组件库；是 FastAPI 返回原生 HTML，原生 CSS、原生 JavaScript、Fetch、DOM 渲染、语义 HTML、浏览器 `<video>` 控件和少量内联 SVG。
- 后来业务认为 POC 页面信息密度过高，默认主视图收敛到全文总结、文案结构、核心标签和前三秒；逐帧证据、A/B 结果和历史评价降为折叠/次级调试视图。

### 2.3 业务反馈转向口播/切片文案（v0.3）

业务明确提出四项：

1. 口播或切片视频需要提炼出文本。
2. AI 拆解文案结构，例如痛点、产品介绍、福利/卖点。
3. 打标签和分类，例如切片、口播等。
4. 分析前三秒。

纯展示/3D 视频暂时不必强行做文案拆解，仍可保留视觉结果；没有音频服务时不能把人工文本或模拟文本冒充自动 ASR。

因此产品主链路改为：上传视频 → 探测音轨 → ASR 或人工/字幕导入 → 视觉证据 + 文本证据 → C10/C20/C30 → 总结、结构、标签、前三秒 → 报告/导出。原视觉 A/B 仍然保留，用于实验和证据回看。

### 2.4 ASR 方向确认

讨论过“多模态模型是否可以直接做音频转写”。结论是把职责拆开更稳定：

- **ASR 模型**负责从音频得到文字和时间戳。
- **视觉模型**负责抽帧图片的画面理解。
- **文本/多模态分析节点**把带时间的转写和视觉证据融合，分析文案结构、标签、前三秒及最终摘要。

用户选择并提供了阿里云百炼的 Qwen Audio 3.1 ASR。项目实际使用模型名：`qwen-audio-3.1-asr-flash-filetrans`。API Key 只写在本机/服务器环境变量中，任何文档和 Git 提交都不包含真实 Key。

## 3. 当前技术栈

### 3.1 服务端

- Python 3.14（当前本机环境）。
- FastAPI + Uvicorn；同源 API、静态资源和 HTML 页面。
- 文件系统持久化，不依赖数据库、Redis、向量库或 Agent 编排框架。
- FFprobe 优先探测媒体；系统没有 FFmpeg 时使用 `imageio-ffmpeg` 随包二进制。
- 图片/视频/音频和 JSON 报告保存在 `data/`。
- 模型调用通过 HTTP 适配器完成；调用记录、模型别名、提示词版本、输入哈希和原始结果会保存。

### 3.2 前端

- FastAPI `HTMLResponse` + 原生 HTML/CSS/JavaScript。
- 没有 npm 构建链、React、Vue、第三方组件库。
- `app/templates/index.html`：页面骨架和模板。
- `app/static/style.css`：高对比度、卡片、A/B 切换、折叠证据、结果页样式。
- `app/static/app.js`：上传、轮询、Fetch、DOM 渲染、结果切换、折叠证据、导出和评价交互。

### 3.3 核心模块

- `app/config.py`：环境变量、采样、模型、ASR、只读和服务器配置。
- `app/main.py`：FastAPI 路由、任务调度、媒体路由、页面和状态接口。
- `app/media.py`：视频元信息、抽帧、FrameSet 和文件存储。
- `app/audio.py`：音轨探测、WAV 提取、FFmpeg/FFprobe 回退。
- `app/transcription.py`：音频资源、转写任务、Provider 选择、标准化和时间戳校验。
- `app/providers/aliyun_asr.py`：阿里云百炼异步 ASR，提交任务、轮询、获取转写 JSON、映射句/词、无语音归一化。
- `app/model_client.py`：视觉/文本模型 HTTP 调用、请求记录和解析。
- `app/workflows.py`：旧视觉 A/B 工作流。
- `app/content_workflows.py`：C10/C20/C30 文案和音画工作流、报告合成、证据正常化。
- `app/content_contracts.py`：`content-1.0` 报告契约验证。
- `app/content_media.py`：视觉/文本证据拼接、历史报告素材。
- `app/contracts.py`：旧 A/B `schema 1.1` 结构验证。
- `app/reporting.py`：JSON/Markdown 导出。

## 4. 视觉工作流的实现约定

### 4.1 抽帧

短视频默认按约 1 秒采样；为了控制 POC 成本和调用次数，单视频最多 20 帧，较长视频会自动增大间隔，并显示实际采样间隔。FrameSet 保存采样时间、帧 ID、图片路径和 SHA-256。

### 4.2 A 分支

- 每一帧独立发送视觉模型。
- 每个结果必须带对应 `frame_id`、时间、字段值、basis/说明和 evidence 引用。
- 所有逐帧结果再交给共同汇总节点，生成全片主题、视觉风格、内容结构、运镜/镜头判断和参考提示词。
- A 不读取 B 的结果，也不额外偷偷发送 B 的图片。

### 4.3 B 分支

- 以同一组图片按时间顺序组成一个有序多图请求。
- 模型需要同时给出逐帧静态证据、相邻帧变化/动作发展和连续片段。
- 连续片段必须完整覆盖所有采样帧，按起止帧排序，片段边界只能被相邻采样点夹定，因此是近似时间范围，不能当作剪辑时间。
- B 的主 UI 展示片段卡；片段内的静态帧和变化证据默认折叠，可展开查看。
- B 的汇总只消费 B 的结构化结果。

### 4.4 旧视觉验证边界

旧 A/B 的结构证据包括帧覆盖、Schema、证据引用、共享图片哈希、调用数和导出端点；这不等于语义准确率。真正的镜头/动作质量必须由业务对原视频逐项评审。

## 5. v0.3 内容工作流

### 5.1 节点职责

- **C00_COMMON**：定义输入来源、证据边界、未知策略和通用输出约束。
- **C10_VISUAL**：以抽帧视觉观察为主，结合对齐的 ASR/字幕，输出画面与文本在时间上的关系；没有可靠证据不推断。
- **C20_COPY_TAGS**：分析口播/字幕结构，重点输出痛点、产品介绍、卖点/福利、目标人群、行动/CTA、内容结构和主题标签。
- **C30_HOOK**：严格只读取 `0 <= t < 3s` 的证据；如果首句跨过 3 秒边界，输出 `limited` 或 `unknown`，不能把后文当前三秒。
- **C90_REPAIR**：对模型不符合 JSON Schema 的结果做受限修复；不创造新事实。

### 5.2 输入来源

- `asr`：真实供应商转写。
- `manual`：人工输入原文，仅用于没有 ASR 时联调，必须显式标记。
- `subtitle`：导入 SRT/JSON/纯文本字幕。
- `fixture`：测试替身，不能当真实识别结果。
- 视觉证据始终来自当前视频的 FrameSet / A/B 结果。

### 5.3 报告字段与证据

`docs/schemas/content-analysis.schema.json` 定义 `content-1.0`。报告保存：视频和 FrameSet、转写 ID、输入模式、语言、时间粒度、segments/words、C10/C20/C30 状态、summary、copy structure、topic tags、hook、evidence references、模型/提示词版本、warnings、provider record 和 usage。

每一个结论尽量带 `basis` 和 `evidence_ids`；没有足够证据时允许 `unknown`、`limited`、`unavailable`。报告完成状态可以是 `completed` 或 `partial`，后者必须说明缺失节点或时间边界原因。

## 6. 阿里云 ASR 的真实实现

### 6.1 API 流程

`app/providers/aliyun_asr.py` 调用：

1. `POST https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription`，带 `Authorization: Bearer ...` 和 `X-DashScope-Async: enable`。
2. 请求体包含模型名、文件 URL、channel_id、language/vocabulary 等参数。
3. 轮询 `GET /api/v1/tasks/{task_id}`，直到完成/失败。
4. 从任务结果中的 `transcription_url` 获取 JSON。
5. 映射句级和词级时间戳，保留远端任务号、request ID、usage 和经过清理的 provider record。

服务器把视频转成缓存的单声道 16 kHz WAV，通过受控路由 `GET /api/videos/{video_id}/audio` 提供给供应商。公网只读实例不开放上传和模型调用；只有 loopback 管理实例使用 ASR 和视觉模型配置。

### 6.2 无语音处理

供应商返回 `ASR_RESPONSE_HAVE_NO_WORDS`、`ASR_NO_SPEECH`、`NO_SPEECH` 或 `NO_AUDIO` 时，任务归一化为 `no_speech`，不是系统错误。此时视觉分析可以继续，C30/口播相关字段应标记 limited/unavailable。

### 6.3 已知时间边界

词/句时间戳来自 ASR，不是剪辑真值。C30 严格使用 0–3 秒；如果第一句跨越边界，报告说明边界受限。视觉和音频时间不能因为采样稀疏而被误写为精确镜头切点。

## 7. 已部署环境

### 7.1 服务器

- 主机：`121.41.164.222`，root SSH；私钥在开发机 `D:\环境配置文件集\fbt2.pem`，不提交。
- 远端应用目录：`/opt/frame-lab-share`。
- 运行用户：`frame-lab-share`。
- 公共 systemd：`frame-lab-share.service`，loopback `127.0.0.1:18767`，只读环境。
- 管理 systemd：`frame-lab-asr-admin.service`，loopback `127.0.0.1:18768`，`APP_READ_ONLY=false`，允许真实 ASR/分析。
- Nginx 前缀：`/poc-review-c15a6fdf7ead48fb8505/`；代理层 `limit_except GET HEAD { deny all; }`。
- `/etc/frame-lab-share/asr.env` 和 `/etc/frame-lab-share/model.env` 是 root:root 600，只在服务器读取真实凭据。

### 7.2 部署原则

公网分享只用于评审已完成结果，不放模型 Key、不接收写请求。需要真实分析时通过 SSH 访问管理实例的 loopback 接口；结果写入同一 `/opt/frame-lab-share/data`，公共实例重启后即可读取。

服务更新的一般步骤：在本地修改并验证 → 通过安全复制同步代码/文档 → 远端 `systemctl restart frame-lab-asr-admin frame-lab-share` → 检查 `systemctl is-active`、health、capabilities、报告 GET、JSON/Markdown 导出和写入 403。

## 8. 回归素材与结果索引

### 8.1 旧三条素材

| 文件 | 视频 ID | ASR Job | Transcript | Content Analysis | 结果 |
|---|---|---|---|---|---|
| `口播.mp4` | `video_41cab5fb16bc40539ac240278815afc2` | `tj_4921780cdd8f423f925523a98f9432bb` | `tr_ed633f209b2944ab9eb0bac41e2d91e0` | `ca_cc4556eb2c8d4ae292fd4f257fbf4fd0` | ASR 2 段/168 词；内容报告 completed |
| `切片.mp4` | `video_02c821c048a14a5c9e0de5b01dcd3448` | `tj_28c609999c2b44b6b81feea28d8763c8` | `tr_ad9fa80db33e432fb896f8b6835c1e21` | `ca_0262b33587fc47429b4ba83b2ec03017` | ASR 8 段/90 词；前三秒跨边界，报告 partial/limited |
| `3D.mp4` | `video_225d45ddfe834732aa7bfced75054dd6` | `tj_f159c78ce022406f946480f60889cfd0` | `tr_9edceec219504b48864df12bfe7786fd` | `ca_73cfee9f24064030aeb350117f02913e` | ASR no_speech；视觉/文案节点完成，口播相关 partial/limited |

### 8.2 2026-09-24 新增两条素材

两条文件在本地 `测试数据集/` 中确认是当日新上传（旧四条文件修改时间为 2026-09-23；新文件修改时间为 2026-09-24 11:51:42），并做 SHA-256 确认。

| 文件 | 本地 SHA-256 | 尺寸/时长 | 视频 ID | ASR Job / Transcript | Content Analysis | 结果摘要 |
|---|---|---:|---|---|---|---|
| `5-43.78.mp4` | `73EB1651A0584A4A45E34E53BD682CB610D00C2AC4E5E3E4C7DB0C6127CDF470` | 30,935,751 B；44.51 s；1080×1920 | `video_651076bb5f9d4e9a83623a932a63806e` | `tj_16ac42c2ab3f48078d33adafe37e002d` / `tr_c9f52ef795df4d7d84418ae5f7512e7b` | `ca_786d428efb4a43b0afec5e28e0928eb7` | ASR 2 段/133 词；跑鞋/新手选鞋/稳定回弹；首句跨 3 秒，partial/limited |
| `1-47.13%.mp4` | `72294AEA68FD6B5522FFE10DC88BA4B48C13DFF30EEFDC7988AED4F4930C9E3D` | 5,520,709 B；19.75 s；540×960 | `video_43397b025b9242619d35988513316ef9` | `tj_bbb42536d8664859ba571c7d9371f64e` / `tr_335655247ab64c1db7187d276e7b5796` | `ca_0d5402027fc344168170cd1c5577774b` | ASR 1 段/58 词；无锡马拉松、磨大腿、运动短裤；C30 completed |

两个新增报告均通过 `app.content_contracts.validate_report`，公网只读列表中的 ASR 内容报告总数为 5（旧 3 + 新 2）。回归证据见 `docs/evidence/new-video-regression-2026-09-24.json`。

## 9. 已解决的问题

1. **抽帧少一帧**：非整数采样率/FFmpeg 时间处理导致少帧，已修正并在 A/B 覆盖检查中验证。
2. **A/B 页面混显**：改为独立方案切换，B 改成连续片段主视图，帧证据折叠。
3. **页面对比度过低**：工作区背景、正文、边框和标签颜色已加深。
4. **公网误触发模型**：公网 Nginx 与应用层双重只读；真实任务移到 loopback admin service。
5. **整段视频 URL 作为 ASR 输入不稳定**：增加受控 WAV 路由，供应商得到标准单声道音频。
6. **无语音被当成异常**：将已知 no-speech 供应商码映射到正常 `no_speech` 状态。
7. **内容报告契约失败**：模型把 `metadata_origin_hint` 当成证据 ID，而报告元数据使用 `origin_hint`；增加受限 `_canonicalize_metadata_evidence`，只映射已知别名并记录 normalization。
8. **前三秒跨界**：C30 不越界“补全”，报告明确 `limited`/`unknown`，保留 boundary warning。
9. **中文 ASR 词间空格**：原文展示保留供应商词切分；下游摘要按时间/词序归一，不将空格误认成语义停顿。
10. **3D/纯展示无口播**：视觉和内容结构仍可运行，口播/前三秒按证据不足输出 limited/unavailable。

## 10. 当前限制和未完成项

- 没有带业务标准答案的 ASR WER/CER、文案结构准确率、标签准确率和前三秒准确率；目前是工程回归，不是业务质量认证。
- 未接入音效、语调、背景音乐、情绪、说话人分离或声画节奏模型。
- 没有自动镜头切分真值；B 片段边界由稀疏采样帧近似。
- 没有视频生成模块；当前只生成分析报告和参考提示词。
- 没有批量队列、数据库、多租户、权限系统、生产级对象存储或高并发优化。
- 公网实例是临时只读评审环境，不应当作为生产服务；管理接口只应通过 SSH/loopback 使用。
- ASR/视觉供应商依赖网络、额度和模型版本；服务配置中的真实 API Key 不在代码仓库。

## 11. 接手后的常用操作

### 11.1 本机启动

```powershell
# 在项目根目录，先配置本机 .env（不要提交）
./run_local.ps1
# 默认 http://127.0.0.1:8765
```

### 11.2 本机验证

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py'
.venv\Scripts\python.exe -m compileall app scripts tests
.venv\Scripts\python.exe scripts/verify_comparison.py <comparison_id>
```

### 11.3 主要 API 类别

- 视频上传、视频元信息、原视频和 `/api/videos/{video_id}/audio`。
- FrameSet/Comparison 创建、状态、帧图和原视觉 A/B 结果。
- `GET /api/capabilities`：模型/ASR 能力和只读状态。
- 转写：创建任务、状态、读取不可变 Transcript、SRT/JSON/纯文本导入。
- 内容分析：创建和读取 C10/C20/C30 报告、历史列表、JSON/Markdown 导出。
- 评价：业务 review 记录。

完整请求/响应以 `docs/content-analysis-v0.3/SPEC.md`、Schema 和实际 `app/main.py` 为准。

## 12. Git/迁移约定

本仓库保存源码、文档、Schema、提示词、验收证据以及可复现的历史 `data/` 元数据/报告/帧证据。为加快 Git 同步，MP4、MOV、WAV 等媒体文件不进入 Git；测试视频仍保留在开发机的 `测试数据集/` 和本地 `data/`，回归证据保存文件名、哈希、任务 ID 和报告状态。

不提交以下内容：

- `.env` 中的 `MODEL_API_KEY`、ASR Key 或任何真实凭据。
- `D:\环境配置文件集\fbt2.pem` 或任何 SSH 私钥。
- `.venv/`、Python 缓存、日志和临时运行文件。

当前迁移版本不上传视频/音频媒体，`.gitignore` 已忽略 `*.mp4`、`*.mov`、`*.wav`；新的媒体仍需通过外部测试数据或部署资产单独提供。上下文文件本身不包含密钥值，只描述变量名、服务位置和可公开的任务 ID。

## 13. 相关文档索引

- [README](README.md)：快速运行和范围。
- [产品开发基线](docs/PRODUCT_BASELINE.md)：确认/建议/开放项和所有里程碑。
- [视觉 POC SPEC](docs/POC_SPEC.md)：A/B、字段、证据与验收边界。
- [视觉提示词](docs/PROMPTS.md)：旧视觉工作流提示词版本。
- [内容分析 SPEC](docs/content-analysis-v0.3/SPEC.md)：C10/C20/C30、接口和状态。
- [内容分析提示词](docs/content-analysis-v0.3/PROMPTS.md)：节点级提示词规范。
- [内容分析实施计划](docs/content-analysis-v0.3/IMPLEMENTATION_PLAN.md)：P1–P6 计划和当前状态。
- [ASR 接入计划](docs/content-analysis-v0.3/ASR-INTEGRATION-PLAN.md)：真实供应商和部署说明。
- [Schema](docs/schemas/analysis.schema.json)、[内容 Schema](docs/schemas/content-analysis.schema.json)：结构契约。
- [新增素材回归证据](docs/evidence/new-video-regression-2026-09-24.json)：最新两条视频的哈希、任务和报告。

## 14. 版本状态

- 视觉工作流：prompt v0.2 / schema 1.1 的 A/B 结构已实施；旧历史也保留。
- 内容工作流：`content-1.0` 已实施。
- ASR：阿里云百炼真实 Provider 已实施，模型 `qwen-audio-3.1-asr-flash-filetrans`。
- 当前部署：本机开发 + 阿里云只读分享 + 服务器 loopback 管理分析。
- 适合下一步：业务人员对五条 ASR 报告逐条做“原音/字幕/画面/结构/前三秒”人工评分，再决定是否优化提示词、切换 ASR、增加音画融合节点或进入产品化。
