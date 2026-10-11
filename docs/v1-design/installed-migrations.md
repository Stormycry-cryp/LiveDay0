# 安装后仍能迁移

原来的 wheel 只包含 Python 模块，没有仓库顶层的 SQL；安装后 `migrate up` 可能只创建注册表便返回 `[]`，实际没有建立记忆表。本次让发行包携带全部迁移，不再依赖用户当前目录或 Git checkout。

顶层 `migrations/` 仍是唯一 SQL 源码。构建 wheel 时复制为 `liveday0/sql_migrations/`；源码和 editable 安装使用明确的仓库路径。所有迁移命令先完整读取资源，再连接数据库：当前 001–005 必须齐全、版本连续、up/down 同名配对、无重复或空文件。资源损坏明确报错，不能把缺文件当作已升级。数据库有发行包不认识的版本时，up/down 拒绝继续。

已有 Python 3.13、uv 和构建依赖时，构建源包并从源包构建 wheel：

```bash
uv build --offline
```

如果构建依赖未缓存，离线构建会明确失败，需先按正常安装流程准备项目已声明的 hatchling。安装生成的 wheel 后，`liveday0 migrate up/status/down` 使用包内资源；数据库连接和专用迁移角色仍按 README 配置，迁移要求停写。此修复没有放宽有数据时的回退门禁，也没有执行生产升级或部署。

本机 Mac 已有 PostgreSQL 17 工具 `/opt/homebrew/opt/postgresql@17/bin`、Python 3.13 环境中的 psycopg 时，可独立验收安装包：

```bash
uv run --python 3.13 --no-sync python tests/test_installed_migrations.py \
  --wheel dist/liveday0-0.1.0-py3-none-any.whl \
  --sdist dist/liveday0-0.1.0.tar.gz
```

验收把 wheel 离线安装到新 `/tmp/ld0-install-*`，从仓库外以隔离 Python 进程加载；逐字节比较源包、wheel 与唯一 SQL 源码。缺资源、缺整对、缺单向、重复、空 SQL、版本空洞六类损坏都必须在连接数据库前失败。数据库部分新建明确标记、0700 Unix socket、无 TCP 的空实例，只验证 `up → 重试 up → down 005 → up 005` 和状态，再由无超级用户、无 BYPASSRLS、无建库/建角色/继承权限的应用登录保存一份固定合成来源。退出停止本次实例，保留目录及 manifest；不接既有库或真实模型，不运行生活闭环演示。

本次优先核实的另外三项已有定向证据，未重复实现：干净 PostgreSQL 重启后新进程读取与原回执重放；完整请求/明确再解释的幂等和 SQL 失败后重试；更正/删除后旧快照展开失效。对应 `tests/test_local_entry.py`、`tests/test_interpretation_receipts.py`、`tests/test_core_invariants.py` 和 `tests/test_trust_boundaries.py`。输入重试须保持原来源身份、时间和完整请求；批准不跨进程持久化，更正仍受 expected_version 约束。现有证据不证明断电/WAL 故障恢复、网络提交确认丢失或真实模型质量。
