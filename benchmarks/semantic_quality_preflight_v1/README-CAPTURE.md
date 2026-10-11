# 固定评测捕获入口 v4：取消人为限额后的离线收尾

本入口仍服务已冻结的12份虚构评测材料（d01–d06、h01–h06）。当前只完成离线和127.0.0.1假服务验证，真实请求0次，真实模型连接和语义质量均未执行。模型、endpoint、prompt、材料、oracle、prepare.py和原base manifest保持不变。

用户已取消本项目人为调用次数、开发费用预算和Codex周额度停线。本版本移除grant中的max_attempts/max_cny、全局CNY上限、单次CNY上限以及quota文件/周剩余50%门；不调用官方额度工具作为执行前置。**12是评测样本设计数量，完成这些样本即收口，不因取消限额增造样本或循环请求。**

原冻结准备合同/README中有关12次、¥15、周额度停线的文字仅保留为历史证据；本文件与capture-manifest.json是当前执行政策，以此覆盖旧政策。语义必要项/禁止项、来源校验、分轴及失败门不变。失败或UNKNOWN必须先核查，不会自动重试、删账本或再收费。

## 安全触发与版本

真实认证启用与触发仍由用户在安全终端完成。Agent不会读取私有文件内容、显示/复制/注入密钥，也不会启动本地程序代传密钥或发起真实请求。未发布的v3自动候选已完整保存在任务证据automatic-entry-v3-paused/，当前入口没有--automatic或credential-ready命令；取消人为限额不启用该候选。

真实state仍为任务工作区semantic-quality-preflight-v1/capture-v1-new-attempts/；外部content-free capture-v1-new-attempts-started.json防止整个state丢失后被当成新机会。已启动的旧版本state不能换release/grant当作新执行，不删除、不重置、不迁移旧尝试。新版本需要重新绑定非秘密审查/授权材料；旧授权文件保持原样，不伪称已可用于当前版本。

```sh
# 可由Agent运行：无网络、认证或真实state写入。
python3 -I -S -B benchmarks/semantic_quality_preflight_v1/capture.py preview
# 本次新增纯政策检查：只用mock，不访问用户文件/真实execute。
python3 -I -S -B benchmarks/semantic_quality_preflight_v1/test_limits_removed.py
```

## 用户未来执行的三份非秘密前置材料

每份绑定最终capture_manifest_sha256、model:glm-5.3-flash、endpoint:https://open.bigmodel.cn/api/paas/v4/chat/completions。JSON是可信操作者的流程记录，不是不可伪造票据；源码审查记录不能代替真实执行授权。

- code-review：三绑定字段和scope:code_review_only。
- grant：三绑定字段、scope:new_fictional_evaluation_only、case_ids完整且按序列出d01–d06/h01–h06、retain_fictional_captures:true、未来UTC expires_utc。case_ids绑定冻结设计，不是可调调用预算；不能塞入旧max_attempts/max_cny或agent_file_launch字段。
- price：三绑定字段、source:https://docs.bigmodel.cn/cn/guide/start/pricing、input_cny_per_million:0.8、output_cny_per_million:2.8、cache_storage_free:true、operator_verified:true、24小时内UTC read_utc。用于真实usage解释和费用估算，不代表预算上限；价格改变或无法核实要先核查，不能给旧价重贴日期。

不需要quota文件，不从Codex周额度推断是否可发服务商请求。发布与临发送前重验冻结字节、三份前置材料、模型/endpoint与STOP。grant字节被账本采用后固定，换进程/授权/文件不能重放已处理case。

## 私有凭据文件

本机用户文件位置为/Users/chenyunzhe/Library/Application Support/LiveDay0/credentials.json，仓库之外。Agent此前exclusive创建了0700新目录和0600空disabled模板，用户已确认填写；Agent没有读取填写后的内容，也未运行程序读取真实文件。

```json
{"enabled": false, "api_key": ""}
```

用户亲自在本机填写api_key、将enabled改为true并保存，保留目录0700和文件0600。程序仅在用户TTY execute、源码/授权/价格及前例评审门之后读取；默认仍可getpass隐藏输入。显式--credentials-file只携带绝对路径，密钥不进入参数、环境、模型payload、ledger、子进程或错误输出。无dotenv/Keychain/自动探测或失败回退。

路径以逐级dirfd O_DIRECTORY/O_NOFOLLOW打开；文件O_NOFOLLOW/O_NONBLOCK，必须为本有效UID、单硬链普通文件、精确0600，立即父目录本UID且0700，最多4096 bytes。JSON只允许enabled/api_key，无重复键或非有限常量，enabled必须布尔true，key为1–512个ASCII可打印非空白字符。错误只返回静态credential_file_unavailable_or_invalid。preview/captures/stop/finalize不能读取该文件。

错误配置会按既有已启动账本失败合同STOP，故用户安全触发前应自行确认配置；Agent不通过检查正文代为认证。

## 一次发送、费用记录和失败核查

全流程持有非阻塞flock：读账本→前例评审→用户认证→重核前置→预约→单次POST→保存结果。0700目录、0600 O_EXCL/O_NOFOLLOW文件、文件与目录fsync在发送前保存case身份、请求SHA和费用估算；根reservation和独立启动见证保留防重复收费证据。

金额仅作估算统计：原价目/模型能力快照给每case ¥1.2058624、12份 ¥14.4703488，不是用户或服务商上限，也不是实际账单。入口的极小一次调用accounting复用原冻结finish/snapshot中的usage验证，自行实现带锁的一次预约，**不设置_limit或任何CNY阈值**；第二次预约拒绝是防重复发送，不是开发预算。token计数、总和、模型能力、请求/响应大小和timeout等运行验证仍保留。

reserved却没有durable result、HTTP/超时/结构/来源/usage失败、本地中断或UNKNOWN仍立即STOP并保存内容无关静态状态；不退款、自动补发或清理原记录。失败后只能先核查再作单独恢复决定，当前没有自动retry命令。换进程或换授权不能重放同case；人工stop、预约前和transport前STOP检查保留。已进入transport的请求不可撤回，其结果仍保存，后续停止。

只保存validator实际消费的model、三项usage、finish_reason、assistant role/content；不保存headers/reasoning/未知字段/异常正文。合法但语义错误的虚构候选保留供逐条审阅，不冒充PASS。

## 逐条审阅与留出锁

一次用户命令只处理下一份固定样本。下一份须提供前一split的report/review：入口从immutable captures重建report并比对，复用原prepare.py ingest/template/score的完整来源与固定oracle门。前缀每case完整评审通过才能前进；开发六份完整phase PASS才允许h01；最后finalize本地收尾。后三轴不继承提取成绩；真人语义验收不能用fake或Agent判断冒充。

下列是供以后用户安全触发的命令，本轮不运行、不创建新的真实grant/review/price/state：

```sh
python3 -I -S -B benchmarks/semantic_quality_preflight_v1/capture.py execute --code-review /ABS/review.json --grant /ABS/grant.json --price /ABS/current-price.json --credentials-file "/Users/chenyunzhe/Library/Application Support/LiveDay0/credentials.json"
# 捕获成功后只读导出新路径，不覆盖既有证据：
python3 -I -S -B benchmarks/semantic_quality_preflight_v1/capture.py captures --split development --out /ABS/dev-prefix.jsonl
python3 -I -S -B benchmarks/semantic_quality_preflight_v1/prepare.py ingest --captures /ABS/dev-prefix.jsonl --out /ABS/dev-report.json
python3 -I -S -B benchmarks/semantic_quality_preflight_v1/prepare.py template --report /ABS/dev-report.json --out /ABS/dev-review.jsonl
# 人工逐项填写后，下一条execute追加--report/--review；heldout同理加--split heldout。
python3 -I -S -B benchmarks/semantic_quality_preflight_v1/capture.py finalize --report /ABS/held-report.json --review /ABS/held-review.jsonl
python3 -I -S -B benchmarks/semantic_quality_preflight_v1/capture.py stop
```

## 定向验证范围

v2八项reader测试与不受影响的函数/方法证据按哈希/AST复用。本次只验证新政策mock3、受影响凭据边界2、授权3材料/固定case身份、取消费用门后的统计与一次预约、scope不重开、完整假服务12样本与停止守卫。旧原始失败、旧候选、v3失败修复证据均保留；不重跑全20、旧真实小试、其他benchmark或生产DB。真实请求与质量仍为not_run，收尾后等待，不开启其他项目项。
