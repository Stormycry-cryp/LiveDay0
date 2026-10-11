# 固定评测捕获入口 v2（私有凭据文件）

本入口只补现有12例评测的执行期缺口。当前只验过离线/127.0.0.1假服务，**真实请求0次，实际语义质量未执行**。模型、既有prompt、12例、oracle及原`manifest.json`完全不变。旧once runner、已消耗授权、旧尝试标记和旧结果不读不改。

`capture.py`是一份可审的独立脚本，复用现有GLMHTTP/adapter/validator/gate，原子exclusive文件及固定摘要。它不接产品CLI/数据库，不做通用provider框架，不读环境变量、dotenv或Keychain；新增显式选择的私有JSON文件，只由用户以后在终端手动execute时读取。真实命令供以后新授权的**用户终端**运行，当前Agent不得进入execute路径。

## 版本与执行门

`capture-manifest.json`绑定新源码/测试/说明和原评测manifest；原manifest再绑定样本与七个纯合同模块。旧冻结SHA是`d246c704dc56abed4dc9164d23850069cb2e12733d77f6dd83fe7967ef9dd217`。每次真实启动及临发送前重验固定字节；改变代码/prompt/数据/模型/价格就停止，不追改标准重试。

```sh
# 当前可运行：无网络、无认证、无真实状态写入，只输出摘要和未授权状态。
python3 -I -S -B benchmarks/semantic_quality_preflight_v1/capture.py preview
# 新入口定向边界，仅临时127.0.0.1假服务；禁外网/DNS，无execute调用。
python3 -I -S -B benchmarks/semantic_quality_preflight_v1/test_capture.py
```

真实开门分开检查：独立源码审查、新用户授权、新鲜官方周额度、可信操作者核实的官方价格；四者绑定最终capture manifest SHA、国内`glm-5.3-flash`和`https://open.bigmodel.cn/api/paas/v4/chat/completions`。审阅文件不能授予请求权限，JSON是可信操作者的流程记录，不是不可伪造安全票据。代码审阅记录只绑定最终版本；既有用户授权范围不增加，当前没有执行真实请求。

供未来操作者填写的无秘密字段（不是已有授权）：

- code-review：`capture_manifest_sha256`、`model`、`endpoint`、`scope:"code_review_only"`。
- grant：同三绑定字段，`scope:"new_fictional_evaluation_only"`、`max_attempts:12`、`max_cny:"15"`（也可更低，达到预留门即停）、`retain_fictional_captures:true`、未来UTC `expires_utc`。grant字节一经新状态采用就固定，换文件不能刷新次数或预算。
- quota：同三绑定字段和grant同值`max_cny`，`source:"codex_official_get_usage_limits"`、`window_mins:10080`、`remaining_percent:>50且<=100`、UTC `read_utc`距执行/发送不超过5分钟。用官方工具读数，不能用操作系统/账户推测。不可读或到停止线，已启动真实账本永久STOP，不自动恢复。
- price：同三绑定字段和grant同值`max_cny`，`source:"https://docs.bigmodel.cn/cn/guide/start/pricing"`、`input_cny_per_million:"0.8"`、`output_cny_per_million:"2.8"`、`cache_storage_free:true`、`operator_verified:true`、24小时内UTC `read_utc`。价格变化/不能核实即STOP，不能把旧价重贴日期当新核实。

## 全局账本与一次发送

真实state固定为任务工作区`semantic-quality-preflight-v1/capture-v1-new-attempts/`，外部content-free `capture-v1-new-attempts-started.json`作启动见证；没有真实state重选/删除/重置/自动恢复选项。假服务独立临时namespace，不能使用任务工作区或生成真实授权。固定十二次按d01–d06、h01–h06，所有进程共享同一个账本。

全流程持有非阻塞flock：读账本→前一例人工门→认证→重新核实→预约→发送→结果。目录0700，文件0600。目录exclusive领取加`O_EXCL/O_NOFOLLOW`创建、文件和目录fsync在发送前写入当前case/请求SHA/费用预留；根目录reservation和外部启动见证防止丢失局部/整个state被当作新机会。日志是小型固定journal，非全系统事件溯源。不得手动删改它来续跑。

`reserved`却没有durable result视为中断/未知并永久STOP；HTTP/传输/超时/结构/来源/usage/本地中断或未知结果同样停止，尝试和预留永不退款。每次最多一次POST，无重试、重定向、代理或工具调用。首次同case成功也只写一次；换进程/提高预算/换grant不重新获额度。真正的人工拒绝或任一适用禁止项失败永久停止；仅未填写/未判定的人工审阅属于等待，不发下一请求，后续完整审阅不是重试已捕获case。

人工`stop`可以在另一进程等待认证时写终止标记，预约及实际transport前均再查STOP。已越过最后发送检查、正在执行的请求无法撤回；其回执仍保存，后续请求永久禁止，不把取消当退款。缺前置文件、quota/price/冻结校验失败也给已启动的真实账本持久STOP。

认证在所有前置门通过后，仅由用户终端触发；非TTY即拒绝。默认隐藏输入；显式加`--credentials-file /绝对路径/credentials.json`才读用户已启用的私有文件，参数只携带路径，不携带密钥。没有env、Keychain、自动探测或失败回退。Agent不替用户输入、读取、搬运或显示密钥。密钥只暂用于固定HTTPS header，永不进入ledger、模型payload、子进程、错误输出或结果。响应只保留validator实际使用的model、三项usage、finish_reason、assistant role/content；headers、reasoning、未知字段和异常正文都不保存。失败只保存静态状态，合法但语义错误的候选内容仍保留供人工判断。

## 一次填写，随后由用户手动使用

本机建议位置为`/Users/chenyunzhe/Library/Application Support/LiveDay0/credentials.json`，在仓库之外。Agent只exclusive创建空模板，不读取用户填写后的内容，也不保存或转移密钥。

```json
{"enabled": false, "api_key": ""}
```

用户在本机编辑器亲自填入`api_key`，将`enabled`改为`true`并保存。保留父目录0700、文件0600；有些编辑器替换文件时会改权限，需用户自行确认。不要把文件或密钥发到聊天、提交Git、放入评测证据或shell参数。

程序逐级以dirfd和`O_NOFOLLOW`打开绝对路径，拒绝父路径符号链接、`..`、文件符号/硬链接及非普通文件。立即父目录和文件必须属于当前有效UID，权限精确0700/0600，文件最多4096 bytes。JSON只允许`enabled`和`api_key`两字段，无重复键、非有限常量或额外字段；enabled必须为布尔true，key为1–512个ASCII可打印非空白字符。错误只返回`credential_file_unavailable_or_invalid`，不含文件正文/密钥/原异常。

只有execute支持该选项；preview/captures/stop/finalize不会读文件。审批、前一例人工门和TTY检查均先于读取；发送前仍重核额度/价格/冻结版本和STOP。错误文件不会切换到另一认证来源；已启动真实账本会按既有失败合同永久STOP，因此先填写好，再核实新鲜前置材料并执行。

```sh
# 追加到下节的人工execute命令；文件由用户亲自填写启用。
# --credentials-file "/Users/chenyunzhe/Library/Application Support/LiveDay0/credentials.json"
# 新增定向验证只读临时目录中的假密钥，不联网、不执行execute、不读用户文件。
python3 -I -S -B benchmarks/semantic_quality_preflight_v1/test_credentials.py
```

新增版本沿用同一真实账本/启动见证。原20个捕获边界方法的定向证据保持历史范围，不声称全套重新运行；此次只验证新reader和审批/人工/TTY/非execute边界。配置文件读取支持不代表真实模型连接或语义质量通过。

## 逐例人工审核与留出锁

一次命令只发下一例并退出，不会自动跑12例。下一次必须提供前一split的report/review：先只读导出账本成功前缀，再复用原`prepare.py ingest/template/score`。入口从自己immutable captures重建report并比对，评分只读固定副本；不能拿另一份report/前次结果解锁。逐例门检查已捕获前缀每例`extraction_pass`与完整人工证据；partial score的整split标志不被误用。d06后要求开发六例完整phase PASS才允许h01。第12次后还需`finalize`人工收尾；后三轴不继承提取成绩。

以下是独立源码审核、既定新评测授权和新鲜额度/价格核实均通过后的用户操作顺序；Agent未运行execute：

```sh
# 首条：四份无秘密审批/核实材料须由可信操作者准备；运行后用户自己隐藏认证。
python3 -I -S -B benchmarks/semantic_quality_preflight_v1/capture.py execute --code-review /ABS/code-review.json --grant /ABS/new-grant.json --quota /ABS/fresh-week.json --price /ABS/fresh-price.json
# 首条成功后：新名字输出，禁止覆盖旧证据。
python3 -I -S -B benchmarks/semantic_quality_preflight_v1/capture.py captures --split development --out /ABS/dev-prefix-1.jsonl
python3 -I -S -B benchmarks/semantic_quality_preflight_v1/prepare.py ingest --captures /ABS/dev-prefix-1.jsonl --out /ABS/dev-report-1.json
python3 -I -S -B benchmarks/semantic_quality_preflight_v1/prepare.py template --report /ABS/dev-report-1.json --out /ABS/dev-review-1.jsonl
# 人工逐项核对实际新来源和响应，填写review。下一例增加 --report /ABS/dev-report-1.json --review /ABS/dev-review-1.jsonl
# 每条重新核实quota/price，保持相同grant。第七条仍用开发六条完整审核；之后用heldout前缀，同样的prepare命令加 --split heldout。
# 第12条后用heldout六条的report/review进行纯本地人工收尾：
python3 -I -S -B benchmarks/semantic_quality_preflight_v1/capture.py finalize --report /ABS/held-report-6.json --review /ABS/held-review-6.jsonl
# 人工撤回：永久停止此state，不授予重试。
python3 -I -S -B benchmarks/semantic_quality_preflight_v1/capture.py stop
```

## 费用与本轮定向证据

2026-10-11只读核实[官方价目](https://docs.bigmodel.cn/cn/guide/start/pricing)：输入¥0.8、输出¥2.8/百万token；不计缓存/促销折扣。[模型页](https://docs.bigmodel.cn/cn/guide/models/vlm/glm-5.3-flash)列1M上下文/128K输出。为沿用原保守门，采用1,048,576输入+131,072输出，预约每次¥1.2058624、12次¥14.4703488，用户可批准最多¥15。本地累计预留不是服务商账户/账单硬限额；需要账单硬限制由用户账户侧设置，变价/未知usage就停且不回收预留。

冻结请求体实际5918–6137 bytes，adapter硬门80,000 bytes；`max_tokens=4096`，响应读取64,000 bytes；connected timeout最多30秒，不承诺DNS/严格墙钟。官方对话参数页读取超时，未以4096推断思考计费总量，因此费用仍用完整模型能力上界。

首次本地假服务17项：16通过，1项发现子进程生成的评分文件权限过宽；设置子进程umask077后只复核受影响完整12次流程，通过。其余证据按不受影响范围复用；初始失败、候选字节和临时状态保留。fake响应和fake人工PASS仅验证门禁/协议，不是语义质量。无旧诊断/样本评测重跑、API密钥、真实POST、产品DB、部署或合并。

独立窄审还指出真实前置失败必须持久STOP、认证等待后预约/发送必须重查STOP；修复后只补三个pure-helper/fixture守卫（前置seal、等待中stop、同grant累计预算耗尽），不调用真实execute/getpass。

实现完成后停止此真实评测分支，等待新的明确执行授权；不自动开启同义召回或另一产品方向。
