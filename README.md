# wb2hub

WorkBuddy 多账号网关的**私有独立维护版**，基于原 workbuddy2api-hub v2.1.1 运行源码恢复。支持原有 OpenAI 兼容接口、账号管理面板与任务调度。

不是原项目官方分支，不保有原 Git 历史。来源与许可见 [NOTICE.md](NOTICE.md)。默认镜像从源码构建 Go TLS helper；Linux amd64 为运行验收平台，Linux arm64 纳入交叉编译检查，未做 ARM 真机验收前不宣称完整运行支持。模型服务和福利由上游提供，部署网关不代表拥有或替代上游服务。使用前核对平台条款和账号风险。

## 本地部署

```bash
gh repo clone brucelau1987cn/wb2hub
cd wb2hub
cp .env.example .env
# 编辑 .env，设置独立且足够强的 API_KEY。
docker compose up -d --build
```

默认仅绑定 loopback。浏览器通过本机 `http://127.0.0.1:7863/` 或安全隧道访问，模型 base URL 为 `http://127.0.0.1:7863/v1`。首次使用立即设置强面板密码；API key 与面板密码是不同的访问控制，不把 key 放进分享链接。需要远程使用时先配置 HTTPS 和访问控制，不直接暴露管理面板。

账号通过面板添加，持久数据保存在 `accounts/`、`usage/`、`subbridge-data/`，均已忽略。**不要把这些目录、.env、备份或日志提交到 GitHub。**

## 自己更新的流程

1. 在此仓库改源码和对应测试；不得把生产数据复制进仓库。
2. 提交/推送后，Actions 执行无外网、无真实账号的维护版回归和已审查上游测试，构建 Linux amd64 镜像并做空账号健康检查；另执行 Go 检查及 amd64/arm64 helper 交叉编译。
3. 检查 CI 成功，给修改打版本标签/写 Release；发布说明不得包含凭据或真实部署地址。
4. 备份生产账号与用量，检查本地差异，再 `git pull --ff-only`、`docker compose build --pull`、`docker compose up -d`。
5. 验证 `/health`、原有账号数量及一条真实请求。只改 API 端口不等于实例隔离，测试实例不要挂生产数据。

**不默认自动更新生产，不照搬原仓库的不可用更新地址。** `WB_UPDATE_REPO` 指向本仓库仅用于版本检查；私有 Release 没有配置认证时面板可能显示无法查询。当前推荐上述 Git + Compose 流程，不承诺面板一键升级；不要把高权限 GitHub token 随意放进容器。

## 测试

```bash
# 每一步测试都在无账号容器中运行，不挂生产目录。
docker build -t wb2hub:ci .
docker build -f Dockerfile.tests -t wb2hub:tests .
mkdir -p test-results/tmp test-results/upstream
# 与 CI 一致：仅放宽这两个无账号临时输出目录，供丢弃 capabilities 的 root 容器写入。
# 仅用于独立临时测试环境，不要对项目、账号或其他宿主目录 chmod。
chmod 0777 test-results/tmp test-results/upstream
docker run --rm --network none --cap-drop ALL --security-opt no-new-privileges -v "$PWD/test-results:/results" wb2hub:tests python -m unittest discover -s tests -v
docker run --rm --network none --cap-drop ALL --security-opt no-new-privileges -v "$PWD/test-results:/results" wb2hub:tests python scripts/run_upstream_tests.py --logs /results/upstream
```

完整上游测试已归档，但默认仅运行审查过的安全基线，不声称上游全部套件已通过。测试范围、保留的旧行为冲突以及两条测试入口见 [docs/UPSTREAM-TESTS.md](docs/UPSTREAM-TESTS.md)。

国际版保底仅针对精确的 code10034 缺少上游密钥错误或证书校验错误，并遵守原保底开关。先查官方当日热力图；已亮不重复上报，未亮则上报后核验。普通401/403仍视为失败。

**连登点亮、网页会话完成、奖励积分到账分别判断。** 保底不制造上游 API key，也不保证奖励到账。定时任务会在新实例启动时补跑；测试时必须用空目录或明确授权的隔离账号。
