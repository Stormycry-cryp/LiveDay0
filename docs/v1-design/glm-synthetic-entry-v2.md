# 固定合成试验入口 v2：脱敏诊断接线

v2 将响应处理连接到已验收的 `synthetic_trial_diagnostics.py`：HTTP 状态/类型、外层 JSON、服务端 error、内层 JSON、字段结构和语义失败均返回脱敏诊断。诊断通过后复用原 hash 固定的 `validate_response` 完成纯 ProposalBatch 校验，丢弃其中包含原文/来源标识的返回值。终端和临时结果只输出静态诊断、有界 usage、合同校验布尔值及固定预算声明。

## 当前执行状态

真实外部调用未运行。原一次性授权已经消耗，旧 runner、933 字节请求、¥1.25 预算、模型、原尝试目录及结果保持原样。v2 不读环境变量或凭据，没有密钥输入或真实外部传输功能。

`--execute` 要求独立的 v2 源码审阅 gate，然后核实原状态的固定摘要；正确的已消耗状态报 `attempt_already_consumed`，丢失/变化报 `historical_state_unverifiable`。源码审阅不能续期付费授权，删除旧标记不能取得新的执行资格。当前没有创建 v2 审批 gate。未来真实试验需要新执行授权和另行审阅的入口版本。

入口是为本 Mac 原冻结材料准备的，固定依赖位于任务工作区 `implementation-phase2/provider-boundary/`。它校验原 runner、诊断模块和请求摘要；按已经检查的源码字节加载定义，避免导入产品 persistence。它不接入默认产品 CLI。

## 可直接本地验收

已有 Python 环境可直接运行本文件：

```sh
python -I -S -B src/liveday0/synthetic_trial_entry.py --preview
python -B -m unittest discover -s tests -p 'test_synthetic_trial_entry.py' -v
```

预览只输出入口版本、固定请求摘要、模型、预算和未授权状态。测试启动 127.0.0.1 假服务，发送同一冻结合成请求，无 Authorization header。`--local-fixture PORT` 是人工本地验证入口，只能连接 127.0.0.1；它自建临时尝试目录并在退出时清理，不允许指定主机、凭据或旧状态目录。

本地路径会在发送前用原 `claim_attempt` 预留并保留单次标记。成功或失败只发送一次；同一测试状态第二次调用拒绝。非 200 或非 JSON 类型不读取响应正文；其他响应读取上限沿用 65,536 字节，连接和 I/O 有界，超时/传输异常调用新静态分类。失败结果不保存服务端正文、未知字段名/值、引文、来源身份或异常文字。

本轮复用上一轮 21 项纯诊断通过证据，只验证新入口接线和受影响错误路径。入口测试依赖本 Mac 已冻结 runner；材料缺失时明确 skip，跨机器可用的纯诊断测试仍单独存在。真实 API 兼容性、模型质量和账单仍未验收。
