# 输出格式示例

此目录中的 JSON 是人工构造的契约示例，**不是模型对真实业务视频的分析结果**，也不是两方案质量的比较证据。

示例场景：两秒合成片段，在 0 ms 与 1000 ms 采到黑底白色方块，位置分别靠左与靠右。实际连续运动、摄影机状态和音轨均不可仅据两张图确定。

- `single-frame.example.json`：A 一次单帧节点的字段结构。
- `sequence.example.json`：B 的两帧联合分析结构。
- `report-a.example.json`：A 基于文字记录的最终报告结构。
- `report-b.example.json`：B 基于联合视觉记录的最终报告结构。

四份文件使用同一 [JSON Schema](../schemas/analysis.schema.json)。真实应用还必须执行 SPEC 中列出的 ID / 时间戳 / 证据作用域等交叉校验；JSON Schema 本身不能证明内容事实正确。
