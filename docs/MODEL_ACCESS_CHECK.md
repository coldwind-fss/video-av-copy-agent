# 模型接入最小检查

检查日期：2026-09-23。原始结果：[model-access-2026-09-23.json](evidence/model-access-2026-09-23.json)。

## 已执行

使用用户提供且已授权使用的凭据，请求 DeepSeek 官方接口 `https://api.deepseek.com`。

1. `GET /models`：HTTP 200，返回 `deepseek-flash`、`deepseek-v4-pro`。
2. `POST /chat/completions` 单张合成图片：HTTP 200，可解析 JSON，返回帧 ID 对应正确。
3. 同一接口十张有序图片：HTTP 200，可解析 JSON，十个帧 ID 完整且顺序正确。

调用参数：model=`deepseek-flash`，thinking disabled，temperature=0.2，response_format=json_object，image detail=high，max_tokens=1800。

| 探针 | 耗时 | 输入 token | 输出 token | 完成原因 |
|---|---:|---:|---:|---|
| 单图 | 0.808 秒 | 301 | 37 | stop |
| 十张有序图片 | 1.331 秒 | 2155 | 216 | stop |

合成图片为黑底白色方块，128×96 像素，方块位置随编号从左向右变化。单图返回“无法据此确定运动”；十图返回了各帧位置及从左向右变化的概述。

## 证据边界

- 已证明这份凭据在检查时可访问上述官方接口，并完成单图、多图、JSON 的最小调用。
- 这些是极简单合成图，提示词与输出均很短。上表耗时和 token 不可直接外推到商品视频、长字段输出或完整两条工作流。
- 没有测试真实视频、本文正式提示词、逐帧完整字段、品牌改编或业务质量。
- 本次只验证图片顺序和基础内容返回，没有验证复杂运镜的准确性。
- 没有运行新视频生成。

## 官方资料

- [Vision](https://api-docs.deepseek.com/guides/vision/)：当前视觉模型名、图片输入和多图限制。
- [首次调用](https://api-docs.deepseek.com/)：官方 base URL；旧 Flash / Vision 实验名称的兼容路由说明。
- [JSON Output](https://api-docs.deepseek.com/guides/json_mode/)：JSON 模式参数、格式提示和截断 / 空输出注意事项。

文档核对日期同上。实际实施中以响应和本项目运行记录为准。
