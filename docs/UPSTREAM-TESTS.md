# 上游测试归档与离线安全基线

## 来源与边界

- 来源：上游 `v2.1.1`，固定提交 `0230cf9`，本次从已检出的固定快照恢复 `tests/`，不下载浮动分支。
- 完整恢复 **305 个文件**：**290 个 `_test_*` 套件（218 Python / 72 JavaScript）**，另有 runner、共享 helper 和 fixtures。归档文件逐字节保留，不为适配本地语义而改写。
- 原有六个 `test_*.py` 文件保持原样，原本 **52 项 unittest** 继续作为维护版语义的回归入口。另增 `test_upstream_runner.py` 的 **7 项入口契约测试**，不导入 runtime、不连接网关。
- 默认安全基线明确选入 **9 / 290 个套件**；其余 **281 个**是“未选入”，不是“通过”，也不是已完成审查。套件数量不是内部断言/方法数量。
- 本次不修改 `wb_*.py`、dashboard 或生产数据，不通过回退维护版修复来消除上游旧断言冲突。

## 双入口（不能互相替代）

```sh
# 维护版测试：原有 52 项 + 新增 7 项 runner 契约测试
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -p 'test_*.py' -v

# 经过审查的上游基线：供 Docker/CI 使用的正式入口
python scripts/run_upstream_tests.py --logs /results/upstream

# 只列出固定 allowlist，不导入或执行套件
python scripts/run_upstream_tests.py --list
```

上游原始 `python tests/run_all.py` 只发现 `_test_*`，并不会执行维护版 `test_*`；stdlib discover 默认只发现 `test_*`，并不会执行这些上游独立脚本。保留原始 runner 是为了完整归档和人工调查，**不能以其无过滤全跑作为默认安全基线**。

## 明确 allowlist 与静态审查

执行器中的 `ALLOWLIST` 是唯一可执行名单，不接受模糊筛选或任意文件路径。

| 套件 | 价值 / 执行边界 |
| --- | --- |
| `_test_health_auth.py` | health 与 API key 配置一致；内存请求桩，临时 settings，无实际请求 |
| `_test_panel_route_auth.py` | GET/POST 管理路由的 panel/API key 分界、敏感值脱敏；内存 Handler，注入 models/update checker，临时 settings，POOL/SCHEDULER 置空 |
| `_test_http_limits.py` | TCP_NODELAY、listen backlog、413/content-length/chunked 护栏；临时目录和回环端口，含启动临时网关及 2MB 上传；不使用真实账号 |
| `_test_upstream_pool.py` | 连接复用、凭据分池、重试、超时、chunked、TLS、原始头顺序；假上游只绑定回环，自签测试证书和假 token；非 HTTP proxy 回退还会尝试 `.invalid`，必须由 network-none 阻断 |
| `_test_shared_ssl_context.py` | 共享 SSLContext、CA 加载、hostname/CERT_REQUIRED 不退化；连接对象只构造不 connect，证书写入临时目录 |
| `_test_tls_fingerprint.py` | TLS 路由、桥接鉴权、JA3/JA4 和状态脱敏；回环 capture/假 CONNECT proxy；真实域名只作 SNI/CONNECT 报文字段，不直连外部；真实 helper 测试需可执行二进制 |
| `_test_dashboard_handlers.js` | 只读 dashboard，扫描所有内联 handler 的定义与登录入口 |
| `_test_dashboard_theme.js` | 只读 dashboard，内存 DOM/localStorage 桩执行主题及首帧布局，验证可调用性和持久化语义 |
| `_test_dashboard_refresh.js` | 只读 dashboard，内存 promise/API 桩验证刷新合并与 analytics 轮询，不发 fetch |

以上“可离线”表示能在隔离环境执行，并不表示执行器本身提供 OS 沙箱。尤其 HTTP limits 会启动整个临时网关，必须避免在宿主机运行整组。本次已通过下述 network-none 容器验收；后续修改仍需重新执行，不能沿用历史通过结论。

刻意不选 `_test_connection_reuse.py`：虽然自建临时网关，但会驱动更新/额度扫描等路由；`WB_UPDATE_REPO=offline.invalid/none` 不是充分的断网保障。连接池行为由 `_test_upstream_pool.py` 覆盖，避免扩大默认执行面。

### 旧语义冲突：保留但不纳入默认基线

`_test_daily_chat_activity_fallback.py` 的旧契约把 TLS 网页腿失败后的 activity report 当作当日完成，甚至断言旧的 `lastDailyChat` 完成标记。维护版已明确区分“网页完成”“经读回验证的活跃证据”“奖励/成长证据”，活动上报不能冒充网页成功，网页仍可重试。相应维护版测试在 `test_daily_fallback.py` 和 `test_account_correctness.py`。

每日任务、scheduler、checkin、restriction/classifier、stream/tool 等其他上游套件也可能断言旧状态/错误分类，不默认接纳。完整归档用于对照；如发现冲突，保留测试并记录维护版决策，不回退 runtime、放宽凭据门禁或伪造成功来满足旧断言。

## Docker 约定与父级依赖

父级负责构建和运行隔离环境；建议采用已批准的固定 digest Python 3.11 Alpine 基底，安装 `nodejs`、`requirements.txt` 和 CA 证书。**不要在此入口里安装依赖或联网下载。**

运行要求：

1. 将无生产数据的工作树构建为镜像中的 `/workspace` 副本；不要 bind mount 宿主项目、HOME、账号、usage、Docker socket 或生产目录。
2. `--network none`，不使用 host network，不映射端口；容器内部回环必须可用。
3. 只有日志输出目录可挂载到 `/results`。容器项目副本允许临时写入；持久项目不挂进容器，因而不会被测试写入。若要用 `--read-only` 容器根，需额外提供可写临时区并验证所有旧脚本写路径，不能假定上游所有脚本只写 TMPDIR。
4. 提供 Python 3.11、Node（支持 lookbehind/matchAll，建议 Node 20 或更新）、系统 CA（共享 SSLContext 套件要求系统证书数 >10）、requirements 中固定依赖。
5. TLS 完整基线需仓库候选位置之一的 `wb-tls-bridge` 可执行二进制，匹配容器架构并能在 Alpine 运行。候选为 `bin/wb-tls-bridge-<system>-<arch>`、`bin/wb-tls-bridge`、`tools/wb-tls-bridge/wb-tls-bridge`。入口只检查存在及执行位，加载器/架构/动态链接错误由真实套件检测。
6. 运行时不传入 API key、token、代理或现有 HOME。执行器对子进程重新构造环境，不继承 `WB_*`、代理、密钥、Python hooks；HOME/accounts/usage/XDG/TMP 指向每套件临时目录。默认强制 `WB_TLS_FINGERPRINT=python` 避免池/CA 套件自动起桥，指纹套件在需验证处自行显式切换。

父级镜像构建好后，容器内准确命令：

```sh
cd /workspace
python -m unittest discover -s tests -p 'test_*.py' -v
python scripts/run_upstream_tests.py --logs /results/upstream
```

如需独立执行选中子集：

```sh
python scripts/run_upstream_tests.py --logs /results/upstream-unit \
  --suites _test_health_auth.py _test_shared_ssl_context.py --timeout 30
```

## 日志、JSON 与失败约定

- 默认串行执行，每套件独立进程、独立临时环境、默认 **180 秒** deadline；`--timeout SEC` 可覆盖，拒绝非正数及非有限值。
- `--logs DIR` 中总是保存各套件 `<name>.log`、按执行顺序拼接的完整 `run.log`、机器可读 `summary.json`；若未提供目录，自动建临时日志目录并打印路径。
- JSON 包含 `upstream_total=290`、`allowlist_count=9`、`selected_count`、`completed_count`、`complete`、`ok`、每种状态的 `counts`，以及每套件状态、退出码、用时、日志路径、错误、`skipped_tests`。运行前先写未完成 summary，每套件后原子更新，避免残留旧的成功汇总误导 CI。
- `passed` 仅用于退出码 0 且无 unittest skip 的完整套件；`failed` 是非零套件退出；`error` 包括依赖缺失/启动异常；另有 `timeout`、`skipped`、`interrupted`。
- 缺少 Node、Python helper 或 TLS bridge **明确 error，整体非零**；不会当作通过。若上游 unittest 内出现 `OK (skipped=N)`，该套件整体标记 `skipped` 并使基线失败，JSON 保留跳过数，不能称全绿。
- `--suites` 后不带文件名（空集）、未列入 allowlist 的已知上游套件、拼错文件、路径穿越、重复文件名均拒绝，退出 **2**。
- 所选套件全部完整通过退出 **0**；任何失败/error/timeout/skipped 退出 **1**；参数/日志 I/O 错误退出 **2**；交互中断退出 **130**。超时/中断会清理当前进程组；未启动的套件保留为未完成，不伪报通过。Linux 容器销毁是对独立 session helper/后代的最后清理边界。
- 不宣称 Windows/隔离容器总 deadline 已验证；父级 CI 应再设置外层 job/container 超时。

## 本次实际验证与未完成项

已在宿主仅执行经审查的无真实 socket/无上游调用单元套件：

```sh
PYTHONDONTWRITEBYTECODE=1 python scripts/run_upstream_tests.py \
  --logs /root/.hermes/cache/scratch/wb2hub-reviewed-units \
  --suites _test_health_auth.py _test_shared_ssl_context.py --timeout 30
# 实际结果：2 passed，0 failed/error/timeout/skipped

PYTHONDONTWRITEBYTECODE=1 python -m unittest discover \
  -s tests -p test_upstream_runner.py -v
# 实际结果：7 tests，OK（先观察入口不存在导致失败，再实现并转绿）
```

runner 契约测试用 Python 标准库子进程作无副作用夹具，实际验证日志保存、JSON、超时、启动失败、缺依赖与 skip 识别；不会把夹具成功计作实际上游套件通过。

随后使用 `Dockerfile.tests` 构建无账号镜像，在 `--network none --cap-drop ALL --security-opt no-new-privileges`、不挂生产目录的环境中实际运行：维护版 **59 项测试通过**（原52项+入口7项），选入上游 **9 套件通过、0 失败/错误/超时/跳过**。日志保存于独立输出目录的 `maintenance.log` 与 `upstream/{run.log,summary.json}`。没有执行其余281套件，也未以假DOM测试替代真实浏览器验收。
