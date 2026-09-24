# 视频拆解与音画文案分析 POC

迁移接手请先阅读 [项目完整上下文说明](PROJECT_CONTEXT_FULL.md)，其中记录从最初视觉 A/B 抽帧实验到真实 ASR、C10/C20/C30 内容工作流、云端部署和最新素材回归的完整演进。

基于同一组按时间采样的抽帧，在一个本机工作台里对照两条视频视觉分析流程：

- **A：单帧独立分析** — 每张图分别识别静态特征，再将结构化文字交给共同汇总节点。
- **B：有序多帧联合分析** — 同一请求按时间顺序输入全部抽帧，生成逐帧特征与相邻区间观察，再交给相同汇总节点。

应用严格使用 [POC SPEC](docs/POC_SPEC.md)、[提示词规范](docs/PROMPTS.md) 和 [字段 Schema](docs/schemas/analysis.schema.json)。模型调用只接收图片，不包含视频音轨。

当前已实现“口播 / 切片文案分析” v0.3：音轨探测、受控 WAV、阿里云百炼 `qwen-audio-3.1-asr-flash-filetrans` 真实 ASR、C10/C20/C30 分析、报告校验、导出与历史回载均已接通。设计和验收边界见 [v0.3 SPEC](docs/content-analysis-v0.3/SPEC.md)、[ASR 接入计划](docs/content-analysis-v0.3/ASR-INTEGRATION-PLAN.md) 和 [项目完整上下文说明](PROJECT_CONTEXT_FULL.md)。真实 ASR Key 只存在运行环境，不提交到仓库。

## 在 Windows 本机运行

1. 在 `.env` 中配置模型接入参数。
2. 首次运行会建立 `.venv` 并安装 `requirements.txt` 所列依赖。FFmpeg 使用 `imageio-ffmpeg` 随包提供的 Windows 可执行文件，无需系统安装。
3. 执行 `./run_local.ps1`，然后打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)。

默认监听 `127.0.0.1`。本机数据写入 `data/`；上传视频、共享帧、模型原始调用记录、提示词快照、对照结果、转写、内容报告和评审均保存在这里。迁移仓库会保留可复现的历史 `data/`；`.env`、`.venv`、日志和缓存不纳入版本控制。

## 范围

单用户轻量 POC；最多 100 MiB / 60 秒 / 20 帧；MP4 / MOV；短视频默认每秒取一帧，较长视频会自动推荐较稀疏的间隔以保持在 20 帧以内，并显示实际采样间隔。支持原视频回看、逐帧与相邻区间并排阅读、全片总结 / 提示词、人工偏好记录，以及 JSON / Markdown 导出。不提供视频生成；音轨识别已接入真实 ASR，但仍需业务人工评审识别质量。音效、语调和背景音乐分析不在当前范围。

本地工作流结构检查不代表分析内容已通过业务验收。业务结论应以原视频和业务人员评审记录为准。

## 对照结果核验

对已完成的任务运行结构与证据核验：

```powershell
.venv\Scripts\python.exe scripts\verify_comparison.py <comparison_id>
```

核验器检查 A/B 帧覆盖、Schema、证据范围、共享图片哈希、调用数、报告和导出端点。三条内部测试视频本轮结果与诊断记录见 `docs/evidence/functionality-validation-2026-09-23.json`。

