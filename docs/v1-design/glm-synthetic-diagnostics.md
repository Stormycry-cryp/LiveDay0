# 固定 GLM 合成试验：脱敏诊断

2026-10-09 的固定五字段小试返回 `synthetic_extraction_mismatch`。旧结果没有保存返回字段，因此无法判断它是缺字段、类型错误还是具体值不同；已消耗的单次尝试保持消耗状态。

`synthetic_trial_diagnostics.py` 提供纯离线函数，供以后另行审定的人工试验入口在内存中检查响应。它没有读取凭据、发送请求、保存文件、写数据库或重试的能力。现有已审定 runner 和请求没有改动，也没有接入此函数；未来接线和新真实请求仍需分别审定。

## 分类顺序

`diagnose_response(http_status=..., content_type=..., body=...)` 接受最多 65,536 字节的 UTF-8 JSON 响应。`diagnose_transport_failure(timed_out=...)` 只接受布尔分类，不接受异常对象或异常文字。

| stage | 检查与静态 code |
| --- | --- |
| input | 参数类型或范围错误：`invalid_diagnostic_input` |
| transport | `timeout` / `transport_error`，由未来调用方先分类 |
| http | 非 200：`authentication_or_permission`、`rate_limited`、`server_error`、`http_error`；响应过大或非 JSON 类型另报 |
| outer_json | UTF-8、无效 JSON、重复键；错误内容不返回 |
| provider | 顶层 `error` 非 null：`provider_error`，即使同时带有可用 choices 也拒绝 |
| protocol | 精确模型、单个 choice、stop、assistant、文本 content、无动作或拒绝 |
| inner_json | content 内的 JSON 无效或有重复键，与外层错误分开 |
| schema | 五个必需字符串字段、日期格式与日历有效性、固定枚举、额外字段数量 |
| semantic | 结构全通过后，与固定合成答案逐字段比较；不一致报 `synthetic_extraction_mismatch` |
| usage | 用量单独校验；提取错误优先，用量错误同时保留；字段正确而用量错误时仍判失败 |
| complete | `ok` / `validated_fixed_fields`，仅表示固定五字段和用量通过 |

固定字段只有 `event_kind`、`original_date`、`updated_date`、`confirmation_status`、`source_quote`。字段诊断为 `missing`、`wrong_type`、`invalid_enum`、`invalid_date_format`、`invalid_calendar_date`、`schema_valid`、`match` 或 `value_mismatch`。存在结构错误时，其他合法字段只报告 `schema_valid`，不继续判断其语义。

例如日期正确、但确认状态从固定答案 pending 变成 confirmed，报告指出 `confirmation_status: value_mismatch`；不会保存 confirmed、原始日期或引文。未知字段只计数，不保存字段名、值、嵌套内容或异常文字。usage 只在整数、正数、加总和容量检查通过后输出三个有界 token 数；它不能证明实际账单或费用上限。

## 离线验证与边界

在已有项目 Python 环境运行：

```sh
PYTHONPATH=src python -B -m unittest discover -s tests -p 'test_synthetic_trial_diagnostics.py' -v
```

测试使用固定合成响应，覆盖字段错误、恶意未知键和内容不外泄、两层重复键、HTTP 200 的服务端错误、结构与语义优先级、用量和传输分类。unittest 不加载 pytest 的数据库 fixture。

此诊断不检查来源指纹、证据 span、权限、删除竞态或完整 `ProposalBatch`。这些仍由 `extraction_wire.py`、`ProposalValidator` 和现有显式 GLM adapter 负责。固定字段通过也不表示真实模型质量或产品端到端验收通过。
