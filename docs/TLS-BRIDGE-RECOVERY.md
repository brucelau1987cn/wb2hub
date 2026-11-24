# 内置 TLS 桥：有限恢复与安全诊断

## 范围与策略

仅 `WB_TLS_FINGERPRINT=auto` 且 `WB_TLS_BRIDGE` 为空/`auto` 的 **bundled bridge** 启用恢复。

- 可恢复分类：`start_timeout`、`start_exited`、`start_failed`、`runtime_exited`、`bridge_local_io`、`bridge_local_unavailable`。
- 初次启动不占重试预算。之后每个进程/配置周期最多 **3 次**恢复，依次等待 **30 / 120 / 600 秒**，使用 monotonic 时钟。
- **按需触发**：失败被请求或 health 发现时开始计时；到期后下一条适合走桥的请求才启动。health 只观察，不启动桥，也不等待启动。
- 桥成功 ready **不返还预算**。因此“成功后立刻崩溃”不会无限重启；第三次恢复后再失败即耗尽。
- 耗尽后持续 Python/OpenSSL，直到显式重置/进程重启或 TLS 配置周期改变。配置周期沿用既有 key：模式、桥设置、secret 摘要、CA 设置、平台/架构、SSL factory/连接类和 binary 候选路径；不因请求成功或 health 轮询重置。
- 缺少 binary、错误架构/权限/loader、认证/协议错误、配置错误不自动恢复。`python`、显式关闭、external bridge 与 strict `node` 不进入自动恢复；strict `node` 失败继续报错，不静默放宽为 Python。原有显式 bridge-off 等配置语义保留。
- 上游 CONNECT/TLS 失败产生的 **502** 不等于本地桥故障：只对尚未发送 HTTP 的当前 auto 请求选择 Python，不停止健康桥。

## 并发与请求安全

- 生命周期由进程锁串行裁决，同一时刻只有一个启动者；auto 并发请求在启动中继续 Python，strict node 维持等待/报错契约。
- 每次失效或新启动推进 generation，池键包含 generation。旧空闲连接关闭，旧响应归还时不再入池。
- 旧 generation 的迟到 CONNECT 错误不得终止当前桥；配置变更期间完成的过期启动必须被回收。路由快照不能混用旧地址与新 secret/generation。
- 仅 **CONNECT 失败、HTTP 尚未发送**时允许一次 Python fallback。同一请求不尝试第二代桥。
- 桥接 HTTP 一旦进入发送/读响应阶段，连接失败不重放（包括复用桥连接的断开），避免重复提交。桥接 **3xx** 同样不得以 `PoolBypass` 交回 urllib 重发；改抛 `HTTPError`，保留状态码、响应头与有界正文，关闭原响应，再由调用者关闭错误对象的内存流。`http_json` 将该 3xx 作为不可重试错误，而非 JSON 成功。原 Python 路径的既有 stale retry/redirect 策略不在本次修改范围内。
- 启动前 SSL factory/连接类检查及 discovery 异常也必须携带所属 generation；错误处理另校验调用的 config key。external 快照在锁内验证 config key 并组装路由，旧配置验证失败同时校验 generation/config key，防止迟到错误或配置 A→B→A 误伤新代。
- stderr 排空线程的启动纳入 child cleanup；只登记已成功启动的 drain thread，避免 `Thread.start()` 资源失败后 join 未启动线程打断 kill/wait/pipe-close。

## 诊断字段与日志

`tls_status()`（由现有 health/settings 消费）新增：

| 字段 | 含义 |
| --- | --- |
| `retry_attempts_used` | 本配置周期已使用恢复次数，0–3 |
| `retry_attempts_remaining` | 剩余预算，3–0；非恢复模式下也显示未消耗计数，不表示该模式可恢复 |
| `retry_wait_seconds` | 等待下一恢复窗口的秒数；未排程/已 ready 为 0 |
| `retry_exhausted` | 可恢复故障因预算耗尽而 disabled |
| `auto_retry_available` | 尚可初始尝试或已排程恢复；等待窗口内为 true，不表示立即执行；关闭池/自定义 CA 时为 false |
| `last_failure` | 最近有效代际故障的安全元数据，恢复成功后仍保留 |

`last_failure` 仅包含 reason、generation、`child_state`、`returncode`、异常类型白名单、整数 errno、stdout/stderr 字节计数及 stderr 分类。**poll 在父进程 kill 前读取**：`child_state=alive`/`returncode=null` 明确表示当时仍存活，不能把父进程产生的 -9 当作桥先崩溃。`exited` 才表示观察到子进程已退出；退出码仍不能单独证明根因。

本地 CONNECT 的 `TimeoutError` 与 `EOFError` 分开记录。不保存原始异常文本，不输出 CONNECT header、bridge secret、代理凭据或 upstream body。stderr 常量内存排空，仅保留 `none`/`output`/`panic` 分类和饱和字节计数；原始 panic/栈内容不进入日志或 health。stdout 首行最多读取 256 bytes，此后持续排空，防止未来输出堵住 pipe。Go 源码未修改。

每次有效失效只产生一条 WARN（包含等待/无重试、kill 前状态、安全错误类别），重复请求/health 不重复报警；成功恢复产生 `recovered` INFO。

## 已知边界：不能反推历史根因

Go 当前 stdout 只有 LISTEN 首行，不存在已证实的后续 stdout 日志堵塞。原实现 stderr 使用 DEVNULL，parent-watch 遇 stdin EOF 会退出；过往 panic/退出原因没有保存，不能事后补证。

现有 Python 本地 CONNECT recv timeout 会按 `bridge_local_io` 终止桥；慢上游也可能让仍存活桥被父进程结束。因此“当前没有桥进程”**不能证明 Go 自行崩溃**。本次增加 bounded recovery 与取证，不改超时策略、不宣称已证明历史故障原因，也不放松证书校验。

## 离线验证与 TDD 证据

新用例：`tests/test_tls_bridge_recovery.py`；原指纹/鉴权/502 套件：`tests/_test_tls_fingerprint.py`。

独立复审修正后，维护 **207 项**通过（原 200 + 新增 7），allowlist **9 套**通过且零 skip，Python **3.9.25 的 TLS recovery 27 项**通过。7 个新增用例均逐项观察 RED 后才修复实现：启动前检查（auto/node × 两种 unsupported）、启动 discovery 异常（auto/node × 两处）、桥接 POST 3xx（auto/node × 301/302/303/307/308）、三处 `Thread.start()` 失败 cleanup、external 路由快照、迟到 external 验证错误及 A→B→A 配置周期。redirect 用例穿过真实 `wb_accounts.urlopen/http_json/_retryable` 的 AST 提取实现和回环 CONNECT/HTTP fixture，验证只发一次 POST、不调用 urllib、错误正文及原 response/HTTPError 均可关闭。复审日志另存 `01-startup-{red,green}.log` 至 `07-discovery-{red,green}.log`，总回归为 `maintenance-green.log`、`allowlist-green.log`、`python39-green.log`，allowlist 明细为 `allowlist/summary.json`。未覆盖或重建既有候选运行镜像。

开发期逐轮 RED/GREEN 日志由维护者独立保留，不随源码发布。回归覆盖有限预算、generation、kill 前诊断、配置切换、禁止已发 HTTP 重放、pipe 排空、安全 stderr、启动失败诊断、EOF、路由快照、health 策略、strict external、binary 配置错误、排空后诊断快照及异常链安全。额外边界矩阵验证模式排除、失败重试耗尽、并发单启动和真实 helper kill→恢复。

```sh
python tests/test_tls_bridge_recovery.py
python tests/_test_tls_fingerprint.py
uv run --python 3.9 --no-project python tests/test_tls_bridge_recovery.py
```

真实 helper 测试只连接回环 fixture，捕获 TLS ClientHello，模拟上游 502；没有向外部/生产发送请求。维护 discover 与九套 allowlist 另在 `--network none --cap-drop ALL` 的无凭据测试容器运行，未挂载生产数据。本地源码修改不等于生产部署。
