# 真实 ASR 接入实施计划

版本：`content-asr-v0.1`  
日期：2026-09-24  
状态：**已实施并部署 P5.1–P5.6；云端三条测试视频已完成真实 ASR 与 C10/C20/C30 工程回归，业务语义质量仍待评审。**

本计划承接 [产品基线](../PRODUCT_BASELINE.md)、[文案分析 SPEC](SPEC.md)、[提示词规范](PROMPTS.md) 和 [验收标准](ACCEPTANCE.md)。目标是把真实口播接入现有视频文案分析 POC，不改变现有 A/B 视觉实验的比较方式，也不把音频模型当成整套视频理解模型。

## 1. 已确认的方向

| 决策 | 状态 | 内容 |
|---|---|---|
| 视觉分析 | Confirmed | 继续使用现有 DeepSeek Vision Flash，负责抽帧画面、场景、产品、构图和视觉变化。 |
| 音频识别 | Confirmed | 接入阿里云百炼 `qwen-audio-3.1-asr-flash-filetrans`。 |
| 调用方式 | Confirmed | 北京地域 DashScope 原生异步文件转写；先提交任务，再轮询结果。 |
| 时间粒度 | Confirmed | 首轮要求词级时间戳，同时保留句级时间戳作为标准段落。 |
| 融合总结 | Confirmed | 不新增独立的“音画大模型”；由后端按时间合并 ASR 和视觉证据，再交给现有 DeepSeek 节点完成 C20/C30 与最终总结。 |
| 本轮边界 | Confirmed | 只验证口播、文本结构、标签、前三秒和音画对应；音乐、音效、语气、情绪识别不纳入本轮。 |

阿里云官方样例调用已验证：该 Key 可以提交并完成 `qwen-audio-3.1-asr-flash-filetrans` 任务，并返回文本和句子时间戳。随后真实接入已在云端回环管理实例完成：`口播.mp4` 和 `切片.mp4` 返回词级时间戳，`3D.mp4` 被供应商明确识别为无语音并标准化为 `no_speech`。这证明工程链路可用；业务语义准确性仍需人工评审。

## 2. 目标工作流

```text
视频上传 / 选择已有视频
        |
        +--> 音轨探测与提取 --> ASR 公网文件地址 --> Qwen Audio ASR
        |                                      |
        |                                      +--> 口播段落、词、时间戳、usage
        |
        +--> 视觉抽帧 --------------------------> DeepSeek Vision C10
                                               |
ASR 结果 + 视觉结果 + 视频时间轴 ------------------+
                                               |
                                               +--> C20 文案结构与标签
                                               +--> C30 前三秒分析
                                               +--> 完整总结 / 报告
```

“音画同步”由时间轴完成：例如 ASR 返回 `[200, 1800)` 的口播片段，后端只把同一时间范围内的视觉帧交给 C20/C30。ASR 本身不负责识别运镜和画面主题，DeepSeek 也不直接接收音频字节。

## 3. 接入范围

### 3.1 Provider 适配器

新增真实 Provider，建议放在 `app/providers/aliyun_asr.py`，由 `app/transcription.py` 统一暴露已有的 `TranscriptionProvider` 协议。

提交请求的核心字段：

```json
{
  "model": "qwen-audio-3.1-asr-flash-filetrans",
  "input": {
    "file_urls": ["<服务端生成的公网音频或视频 URL>"]
  },
  "parameters": {
    "channel_id": [0],
    "language_hints": ["zh"],
    "vocabulary": {"品牌名": 5, "产品名": 5}
  }
}
```

请求使用：

```text
POST {ASR_BASE_URL}/api/v1/services/audio/asr/transcription
Authorization: Bearer ${ASR_API_KEY}
X-DashScope-Async: enable
```

适配器职责：

1. 提取并校验 `task_id`、`request_id`；
2. 保存远端任务 ID 后再开始轮询；
3. 按 2–5 秒间隔轮询，在服务端截止时间到达后结束；
4. 远端任务已接受时不盲目重新提交，避免重复计费；
5. 将 `transcripts[].sentences[].words[]` 映射到内部标准结果；
6. 保存 provider、模型、usage、耗时和错误码；
7. 原始响应只保存在服务端任务目录，不返回给浏览器。

### 3.2 音频文件传输

该模型要求 `file_urls` 能从公网访问，不能直接读取 Windows 本地路径。因此接入必须增加一个受控的媒体定位步骤：

- **分享服务器路径**：复用现有视频 GET 路由，生成仅读的音频 / 视频 URL；公网只开放 GET，不开放上传和任务创建。
- **本机开发路径**：配置 `ASR_MEDIA_MODE=oss` 时，将提取后的音频上传到指定 OSS，并只把短期 URL 交给 ASR；没有可用公网地址时明确返回 `ASR_MEDIA_URL_UNAVAILABLE`。
- URL、对象密钥和临时凭证不显示在页面，不写入普通调用日志。

首个真实样本优先使用已部署服务器可访问的文件 URL；如果该 URL 在阿里云侧回源不稳定，再实现 OSS 传输，不改变 Provider 业务接口。

### 3.3 时间对齐

内部标准统一使用视频起点为零的毫秒半开区间 `[start_ms, end_ms)`。

- 读取音轨的流起点、视频起点和提取配置；
- 对 ASR 返回的句、词时间加上 `audio_offset_from_video_ms`；
- 校验时间非负、递增、在视频时长内；
- 词级结果保留在 `segments[].words[]`，句级结果保留在 `segments[]`；
- 如果无法确认偏移，不把结果标成精确的前三秒，只保留 `timing=none` 或 `timing=sentence` 的真实能力；
- 不重写 ASR 原文。人工纠正必须创建新 Transcript 版本。

### 3.4 下游提示词

现有 C10/C20/C30 提示词保持职责不变：

- C10 只描述画面，不猜口播；
- C20 读取标准 ASR 文本和视觉观察，按原文顺序提炼文案结构和标签；
- C30 只收到 0–3 秒内的视觉证据、词 / 句摘录和边界信息；
- 模型不能自行扩展时间窗或补全文外口播；
- 结果中必须显示 `source=asr`、provider、model、`timing=word/sentence`。

不新增音频提示词节点，不把 ASR 结果直接当成最终报告。

## 4. 配置计划

只在本地或受控服务环境配置，不写入文档和前端：

```text
ASR_PROVIDER=aliyun_dashscope
ASR_MODEL=qwen-audio-3.1-asr-flash-filetrans
ASR_BASE_URL=https://dashscope.aliyuncs.com
ASR_API_KEY=<仅环境变量>
ASR_TIMEOUT_SECONDS=180
ASR_POLL_INTERVAL_SECONDS=3
ASR_MAX_RETRIES=2
ASR_LANGUAGE_HINT=zh
ASR_ENABLE_WORDS=true
ASR_MEDIA_MODE=public_url|oss
ASR_PUBLIC_BASE_URL=<可选，服务端媒体只读地址>
```

`ASR_API_KEY` 不进入 JSON、Markdown 导出、调用详情或公网分享实例。`/api/capabilities` 只报告 provider、模型和能力状态，不返回凭据。

## 5. 实施顺序

| 阶段 | 工作 | 可观察结果 |
|---|---|---|
| P5.1 | 配置和媒体定位抽象 | 能判断本次任务是否有可供阿里云读取的 URL；无 URL 有明确错误。 |
| P5.2 | 阿里云 Provider | 单个任务可提交、轮询、超时、失败和保存远端 ID。 |
| P5.3 | 响应标准化 | 句、词、原文、时间、语言、usage 通过 `content-1.0` 校验。 |
| P5.4 | 一条真实视频端到端 | 视频 → 音轨 → ASR → C10/C20/C30 → 报告可完成。 |
| P5.5 | 三条测试视频回归 | 对 `3D.mp4`、`口播.mp4`、`切片.mp4` 分别保留 ASR、视觉、融合结果。 |
| P5.6 | 页面最小调整 | 主视图显示“真实 ASR”来源、口播摘要和前三秒依据；原始逐帧证据仍折叠。 |
| P5.7 | 业务验收记录 | 分开记录 ASR 错误、视觉错误、融合错误和业务可用性评价。 |

P5.4–P5.6 已完成：五条报告均可通过公网只读路径读取，`input_mode=asr`；原三条与新增两条均保留真实 ASR、视觉、文案和前三秒状态。新增素材明细见 [新视频回归记录](../evidence/new-video-regression-2026-09-24.json)，整体融合明细见 [音画融合验收记录](../evidence/content-asr-fusion-validation-2026-09-24.json)。

不在 P5 中改造前端框架、引入数据库、建设队列、增加音效 / 情绪识别或开放公网写入。

## 6. 验收标准

### 工程验收

- 能提交真实 ASR 任务并保存 `remote_job_id`；
- 轮询到 `SUCCEEDED` 时得到非空原文；
- 失败、超时、鉴权失败、远端文件不可访问均有明确错误；
- 词 / 句时间戳通过范围、顺序和视频关联校验；
- 报告来源为 `asr`，不再标为 `manual/subtitle` 或 `fixture`；
- 无音轨、无语音和识别空结果不会混成同一种状态；
- 旧 A/B 报告和人工导入结果保持可读；
- API Key 不出现在日志、导出和页面。

### 业务回归

对三个真实视频分别记录：

1. 口播原文是否可用；
2. 品牌词 / 产品词是否正确；
3. 0–3 秒口播是否被正确纳入或排除；
4. 文案结构是否保持原始顺序；
5. 标签是否有原文或画面依据；
6. 音画合并后的剧情 / 总结是否比纯视觉结果更完整。

业务评价与工程通过分开记录。首轮不预设准确率结论，先形成三个样本的错误清单，再决定是否比较豆包或腾讯 ASR。

## 7. 风险和未决事项

- **公网文件地址**：本模型不能直接读取本地路径，这是第一实施风险；先验证现有服务器 GET 媒体地址，再决定是否补 OSS。
- **口播质量**：噪声、音乐、多人重叠和方言可能影响原文；模型无法识别的词必须保留不确定性。
- **时间边界**：句级时间戳跨越 3 秒时，由词级时间戳决定摘录；没有词级结果时不得伪造边界。
- **模型额度**：600 RPM 只代表接口配额，不代表业务准确率；POC 仍保持单任务串行和真实 usage 记录。
- **分享部署**：公网只读实例不直接配置 API Key。真实 ASR 运行先在本地或受控服务完成，验收后的报告再复制到分享实例。
- **质量结论**：官方样例调用成功只证明连通性；三个业务视频的识别质量、文案结构质量和最终业务可用性仍为 Not run。

## 8. 已实施结果（2026-09-24）

- `app/providers/aliyun_asr.py` 已接入异步提交、轮询、结果下载、词 / 句标准化、usage 和远端任务记录。
- `/api/videos/{video_id}/audio` 已提供受控 WAV GET 路由；公网分享实例继续只允许 GET，ASR 写任务只在服务器回环管理实例执行。
- 云端服务仍使用原 `frame-lab-share.service`、Nginx 路径和 18767 端口；新增 `frame-lab-asr-admin.service` 仅监听 `127.0.0.1:18768`，不改变其他应用。
- 三条测试结果：`口播.mp4` 2 个语音段 / 168 个词；`切片.mp4` 8 个语音段 / 90 个词；`3D.mp4` 0 个语音段并记录 `no_speech`。所有有语音结果的 `timing=word`，来源为 `asr`，模型为 `qwen-audio-3.1-asr-flash-filetrans`。明细见 [ASR 部署验收记录](../evidence/asr-deployment-validation-2026-09-24.json)。
- API Key 只存在云端 systemd 环境文件，不进入代码、文档、JSON 导出或浏览器响应。

## 9. 下一步

下一步由业务分别评审 ASR、视觉和融合三类错误，重点检查品牌词、产品卖点、前三秒摘录和无语音分支。分享实例继续保持只读，不开放公网任务创建。
