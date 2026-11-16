# 来源与许可

本仓库是从已部署的 `ardeyouxipianyi/workbuddy2api-hub` v2.1.1 容器中恢复的运行源码维护快照，原镜像 revision 为 `f5f15d33d401e590b3869017beb480a54a91de7a`。

原项目作者及贡献者保留其著作权。本仓库不是原作者的官方仓库，也不是保有完整原 Git 历史的 GitHub fork。维护版修改不改变原有代码及第三方组件的归属。

## 已核实的上游许可来源

原 Python 项目现可从 [XueyuanPang-Google/workbuddy2api-hub](https://github.com/XueyuanPang-Google/workbuddy2api-hub) 访问。已核对其 v2.1.1（上述原镜像 revision）和 `0230cf9e79347f80cf7d078b033ad2c9063f6509` 中的根目录 `LICENSE`；本仓库按原文恢复该 MIT 许可，不补写版权人姓名或年份。

本次补回的测试、Go TLS helper 源码及配套说明取自上述固定上游提交；来源和文件摘要见 [docs/UPSTREAM-PROVENANCE.json](docs/UPSTREAM-PROVENANCE.json)。该清单记录导入时的原始文件摘要，维护版适配可能另有修改，不表示本仓库与上游全量相同。

`tools/wb-tls-bridge/` 保存 Go 源码、模块锁定文件及第三方许可；`licenses/wb-tls-bridge/` 保留原恢复材料中的说明和第三方许可。默认 Docker 构建从 Go 源码生成 helper，不再以旧二进制作为构建必需品。`bin/wb-tls-bridge` 仍作为原 Linux amd64 保全产物留存，不能据此承诺新旧二进制逐字节一致。

仓库继续保持私有。恢复 MIT 许可来源不等于完成法律意见、改变上游服务条款或授权使用他人的账号、商标及数据；第三方组件仍须遵守各自许可。
