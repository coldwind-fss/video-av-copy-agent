# 文案分析契约示例

这些文件是**人工编写的合成格式例子**，没有调用 ASR 或 DeepSeek，也没有对应的真实视频。它们不是业务验收证据。

| 文件 | 表达的场景 |
|---|---|
| [transcript.fixture.json](transcript.fixture.json) | 10 秒虚构视频的三段句级转写，分别为痛点、商品介绍和优惠引导 |
| [report.fixture.json](report.fixture.json) | 基于上述文字的完整报告格式；优惠出现在后段，不进入前三秒 |
| [report-untimed.fixture.json](report-untimed.fixture.json) | 同样文字没有时间戳：全文可分析，前三秒口播为 unknown，只能分析窗口内画面 |

共同使用 [content-analysis.schema.json](../../schemas/content-analysis.schema.json)。`source=fixture`、`is_fixture=true`；返回模型为空、模型调用清单为空、全零 SHA256 和固定日期仅为格式占位，不代表实际运行。

模型提示词可使用契约结构，不能把示例内容当作新视频事实。将来开发 fixture 适配器时只能显式开启，不因缺模型配置而自动返回这里的文案。

Schema 能检查形状和部分条件，不能单独校验引用是否存在、引文是否与输入一致、时间是否超出对应视频、前三秒是否泄漏后段信息或结构是否覆盖全部文本。实现时必须补充 SPEC 所列交叉校验。
