# 普通痕迹到后来明确事件：有限合成旅程

2026-10-11，基线 `8d037266fdf76293abff07849b7cced1ad97719f`。复用冻结来源解释、原始回执、版本更正和删除撤销，只补普通痕迹的召回出处对应，不引入事件识别规则或新的存储层。

## 冻结合同

[夹具](../../tests/fixtures/ordinary_trace_life_journey.json)与[可运行测试](../../tests/test_ordinary_trace_journey.py)首次执行前经独立只读预检。夹具 SHA256 `dcad712333625c572589cc57e4ea498e7e7ba907a915aed3cdcbd9d156f73c65`；测试 SHA256 `19bb4b6879e3b024f97d19aa871cafd1b96f7267aa21f7acdf95f59d1fb82391`，RED/GREEN 完全一致。

两次顺路经过公园先各自留作普通痕迹，不形成事件、派生理解或被强行放进历史。后来明确决定下周尝试一次散步，独立新来源通过冻结解释提交一个事件：决定发生了，散步尚未执行。此前片段保留原身份、回执和正文，通过脚本明确建立背景关系，不搬移到事件中，也不成为该决定的事实支持来源。

事件提案、背景关联与更正正文全部预先固定。测试执行实际 MemoryService/PG 的保存、召回、更正、删除，不测试提取器自动判断片段何时有意义、自动找关系或泛化慢理解。

## 唯一生产补齐

原 trace summary 用独立数组表示观察、边界与出处，观察文本去重后无法可靠对应身份，也没有发生时间。现在每个 `traces` 元素是一条完整的 life_trace：稳定 ID、observed_only、active 生命周期、evidence ID、来源 occurred_at、观察与观察边界。父摘要 source_refs 是出处索引；旧 observations/observation_boundaries 内容数组不再生成。

时间从同租户原 evidence 读取，不采用后来决定、更正或检索时间。摘要仍按稳定 trace ID 去重，原关系/生命周期/selected 事件门、共享 derived-input 行预算与 remaining+1 SQL 门保持。没有根据公园/散步文字新增生产分支，没有改已有痕迹或复制事件真相。

预算不足时省略完整摘要，保留同一 pinned snapshot 展开句柄，不能切断某条观察的来源或边界。新生成上下文的调用方需读取 traces；既有历史快照不原地改写，已知更正/删除仍使旧快照拒绝。

## 定向证据

基线8项中4 PASS、4 RED；四个RED是同一个出处对应缺口在普通召回、tiny预算展开、更正后与删一个来源后的体现。修复后同一旅程8/8：

- 普通片段只留trace；后来事件身份、时间、正文与独立来源完整，原痕迹/回执不变。
- 冻结解释原样重放回原身份，20张表完全不变。
- 两条痕迹各自保留来源、时间、正文、边界；tiny card预算整体省略后完整pinned展开。
- 更正同一事件到v2，限定“有空试一次”，保留来源、未推出每日承诺；旧快照拒绝。
- 删除第一痕迹来源只擦除指定痕迹，另一痕迹与独立支持的事件完整；旧来源重放和旧快照拒绝。
- 删除事件后旧冻结解释不能再生成，20表零写，未请求删除的另一痕迹保留；不强行召回孤立历史。

RED `/private/tmp/ld0-ordinary-journey-_k51ev0j/manifest.json`，GREEN `/private/tmp/ld0-ordinary-journey-p3fu7pik/manifest.json`。受影响既有derived-input主方法只适配完整records断言，原固定夹具hash未变，9/9通过：`/private/tmp/ld0-derived-input-wwg367ce/manifest.json`。原测试与证据保留，不声称适配后整测试文件仍与旧hash相同。

三次均为新空标记PG、0700私有Unix socket/no TCP、受限应用登录，已停止且PID不存在，日志与停止库保留。仅复验两个unittest方法共17个定向检查，无全仓、旧演示或heldout。冻结解释模块hash `0d50394f1067f53104cfcba57f42fcc435b11a47ba49f796bcd113ded01d41f7` 与已存证据一致，其他已有边界按原证据复用。

## 剩余能力

有限连续性可运行，真实模型完整提取能力仍未通过。唯一旧真实小试仅有synthetic_extraction_mismatch，缺少字段值不能恢复差异，原调用授权已消耗。自动识别同一经历、普通片段的后来关联、新生活线索发现、人物/关系理解仍开放。下一有价值小项是先冻结条件承诺与后来反证的有限旅程，只补实际机制缺口，不扩回工作量审计或生产部署。

运行命令（使用具备psycopg的项目测试Python，测试自行创建并停止新隔离PG）：

```sh
python -B -m unittest tests.test_ordinary_trace_journey -v
```
