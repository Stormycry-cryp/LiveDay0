# 派生支持与 trace 的输入扇出门

接续 `d3786f2` 有限闭合表，本轮只处理召回编译的 projection support 与 trace 输入行数，不扩到全SQL优化、mention候选或新调度系统。

## 合同

`RecallOptions.derived_input_limit` 默认256、最大4096，非布尔正整数；CLI `--derived-inputs 256`。一次有限召回周期内，**所有选中派生视图的支持引用槽与不同身份的trace行共享剩余额度**。同一canonical事件给不同projection、或同一projection中的support/counterevidence角色各占引用槽；counterevidence仍在完整body的版本引用中保留。trace按稳定trace ID去重：同一trace连接两个选中事件只占一行，不靠观察文本相似度去重，不改变原语义卡身份。

原projection SQL在顶层LIMIT前聚合完整support数组，即使查询无关也读取整个支持集合。现在先取既有有限projection envelope（默认24），通过EXISTS获取“连接selected事件”与“连接已知unsafe事件”的两个资格信号。沿原active、相关性、unsafe及每类6条门选择后，才加载其完整support槽；SQL最多返回**剩余+1**行。trace沿原tenant、active关系/trace、selected事件及DISTINCT身份条件，同样以剩余+1行读取。多出一行只检测溢出，检查在映射/summary装配前进行，没有部分支持或部分trace摘要落入context。

超限抛`RecallWorkLimitExceeded('derived_input_limit')`，CLI exit2静态`error=recall_work_limit_exceeded`、`context_available=false`，无正文、部分出处或新snapshot。超限封闭整个周期；外层捕获后重入、SQL或退出检查仍保留该错误类别，不能误分类成pending的“待维护”。嵌套周期不能增加剩余预算，新周期finally复位后才恢复额度。

不删除、裁切、吸收或重新分类超限材料。使用一个新的较大有限额度可完整读取；本次3 support+3 trace示例由cap5拒绝、cap6恢复。超过配置最大值的数据仍明确拒绝，不能承诺自动恢复。本门属于读取输入门，不自动创建维护任务。编译事务拒绝时不发布snapshot；测试中的持久生活表不变，但此前合法catch-up可能已提交，不扩称所有recall都零写或全局回滚。既有返回预算仍可省略完整卡并保留pinned展开引用，这与输入阶段静默截断出处不同。

## 受控反例与验证

[固定夹具](../../tests/fixtures/recall_derived_input_cases.json)执行前经独立只读预检，SHA256 `de85cf9ad830d8cde892cfe51093ae6ad5a8c3ffbb32b75f066e30889c5e7bd8`。三个规范事件；两support与一counterevidence支撑一派生视图；三个trace，其中一条跨两个selected事件。

基线同夹具9检查：8个新工作门检查RED，既有真实SQL故障原子性1PASS。cap2/3/5仍返回完整可用context，support聚合3槽、trace取3行且无输入LIMIT；无关查询也先聚合3槽。基线用旧接口真实路径，不给不存在的参数制造错误。RED `/private/tmp/ld0-derived-input-jdephcxo/manifest.json`。

[定向测试](../../tests/test_recall_derived_input.py)主9检查GREEN `/private/tmp/ld0-derived-input-oxp1pyis/manifest.json`：

- cap2在support第三条哨兵拒绝，不读trace；cap3的trace只取1条哨兵拒绝；cap5取3 support+3 trace拒绝。
- cap6完整返回全部支持ID、counterevidence版本、uncertainty，以及3个trace source ID、观察和边界；跨事件trace没有重复。无关cap1无历史/0support与trace载荷读取。
- 拒绝和成功读取前后11张生活表逐行不变；真实`SELECT 1/0`发生在support读取后，不发布snapshot，恢复查询路径后cap6成功。
- 实际CLI静态exit2；明确更正使projection失效，删除trace来源后只剩2个完整出处，旧pinned展开拒绝。

无I/O方法验证0/负数/bool/4097、共享nested消耗和封闭周期的错误分类。由于资格SQL改动，另一个小型隔离回归故障注入stale active projection与unsafe support：该视图和unsafe事件不返回，不读其支持载荷，unaffected trace出处完整；证据见任务目录的`affected-unsafe.json`。受影响旧pending nested/noI/O单方法通过；其余旧证据按哈希复用，不重跑全仓、旧演示或heldout。

全部实例为新空标记PG、0700私有Unix socket/no TCP、受限应用登录；均停止且PID不存在，保留RED、日志和旧数据。没有模型请求、凭据、真实生活库、部署或合并。

## 回到产品里程碑

后续[普通痕迹旅程](ordinary-trace-journey.md)将摘要的独立内容数组改为逐条traces完整记录（身份、出处、发生时间、观察、边界）。固定夹具和共享行预算保持；只适配主方法摘要断言，9项定向复验通过，旧RED/GREEN物保持原样。旧证据证明当时摘要形态，不与新测试文件冒充整文件hash一致。

本轮输入门完成后回到[产品能力现状](user-capability-status.md)，不自动执行有限闭合表中的其他缺口。support/trace传输行数已有界，但SQL扫描/EXISTS工作、单条body/delta/trace字节、global unsafe ID、mention聚合、维护失效扇出及持久增长仍开放；默认800ms协作deadline也不是严格墙钟。本批不证明长历史整体资源封顶。

原第二优先的泛化生活输入、普通痕迹形成事件/生活线索、人物及关系理解仍未完成。固定合成adapter与人工确认只证明保存门；原唯一真实小试仍为`synthetic_extraction_mismatch`。新的真实模型质量验收需要事先冻结独立评测及新的明确调用授权，不能重试已消耗调用。原第四优先服务端身份、跨设备、生产增长与恢复仍未验收；真实部署/生产环境操作需要独立授权。本轮没有这些权限，也不靠继续添加合成成功用例宣布产品已可长期托管真实生活。
