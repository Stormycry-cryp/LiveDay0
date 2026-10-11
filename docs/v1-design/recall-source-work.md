# 选中卡片的来源引用工作量

接续`ee7d5bd`生活输入质量门补项，泛化真实提取继续未验收，按用户授权回到原第三优先。现有48候选/12选中和返回大小预算不能约束一张卡所挂出处的数量；本批只补来源引用行的数据库传输与Python装配门。

## 合同与入口

`RecallOptions.source_reference_limit`默认256，最大4096，正整数。它是一次召回所有选中规范卡片的**总引用行**上限；共享同一evidence的不同卡引用仍分别占行，不是每张卡256，不把不同role/来源折叠丢失。先做相关性评分和unsafe排除，才给selected IDs加载来源，无selected时不查询来源。

```sh
uv run liveday0 recall "$TENANT_ID" "某次经历" --source-refs 256
```

SQL按原tenant/status条件与来源时间排序，最多返回`limit+1`条元数据行。多出的一条只检测溢出，不将部分列表装配成卡。超限API抛`RecallWorkLimitExceeded('source_reference_limit')`；CLI沿原静态错误合同exit2、`error=recall_work_limit_exceeded`、`context_available=false`，不返回记忆正文/SQL/部分出处。编译事务回滚、不创建新召回快照；此前已经合法提交的catch-up不因此回滚。原始记忆不删不改，背景维护仍可处理，调用者也可显式选择较大的有限上限。

恰好上限的来源集合完整保留，仍按原完整返回预算选择整卡或带pinned展开引用的短形。256是本次预先写明的保守运行上限，不是实测生产容量/SLA或模型token数。完整展开沿已有64KiB字节/800ms协作deadline合同，不宣称其整个载荷所有维度都有本行上限。

## 固定反例与证据

[夹具](../../tests/fixtures/recall_source_work_cases.json)在首次运行前独立预检，SHA256 `37d1cc2de76576e8ad635b03d2c90904711deea4faa5e1eaddd6e374ef5e599a`执行后未改。三张合成事件分别9/4/4条出处。

基线没有来源工作限额，5次查询包括无关问题都先传输17条出处。新合同6个检查均RED，这记录的是缺失运行门，不伪称原版本存在source-refs参数。修复后同夹具6/6通过：9来源/cap8整次拒绝（只读9 sentinel），cap9完整9；两4来源事件共享cap7拒绝/只读8，cap8完整两卡8引用；cap1无关查询空历史且0来源SQL；真实CLI静态exit2。成功时逐项验证完整source ID集合及边界/不确定性，失败无新snapshot，生活相关五表逐行不变。

[单项](../../tests/test_recall_source_work.py)RED `/private/tmp/ld0-source-work-7jfvakns/manifest.json`，GREEN `/private/tmp/ld0-source-work-7kwu3sa2/manifest.json`。另一个新PG仅跑受影响旧cue单项，观察器从原source_map移到实际canonical候选SQL，48/8 cap、旧/近期/foreign/closed/空历史/去重仍通过，`/private/tmp/ld0-oldcue-rjlldt23/manifest.json`。0/负数/bool/4097配置单方法在无I/O下拒绝；生产逻辑GREEN后未改，不重跑PG。所有新标记空PG/私有0700socket/no TCP/受限角色，均停止且PID不存在，证据保留。

## 未解决的完整工作量部分

这不是整个召回的硬资源上限。SQL排序/扫描/FTS/vector discovery、规范body与source_kind字节、pending增量、全局unsafe检测/catch-up触达、派生支持集合/mention候选/trace聚合等仍有未完成的工作量门。现有statement_timeout取消和服务deadline继续有效，单步Python不能硬中断。不能将小组GREEN改称长历史或高连接节点整体完成。

下一项优先检查pending增量与read-before-use追平的有限工作边界；复用既有时序/失败预算证据，定向超限必须保持最新事实安全，不返回已知旧结论、不烧维护失败预算，不添加模型调用或全量扫描重审。
