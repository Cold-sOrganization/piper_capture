# Piper + D435i 采集程序迁移包

这是 2026-09-18 从实际部署机器复制的程序和运行依赖快照。原 GOS `/home/user/piper_capture` 和中转机 `/home/ccic/piper_capture` 保留原位。本目录不会自动启动第二个采集服务。

## 目录

| 目录或文件 | 内容 |
| --- | --- |
| `gos/program/` | GOS 采集器、CAN 解码、相机绑定、控制入口、启动脚本 |
| `gos/runtime/` | vendor-python、ARM64 RealSense 2.51.1 动态库、USB 权限规则 |
| `gos/environment.json` | GOS 系统信息、系统依赖版本、原始源码 SHA-256 |
| `relay/program/` | 中转机接收、导出、审阅程序和测试 |
| `relay/wheels/` | 中转机 Python 3.10 / Linux x86_64 离线安装包 |
| `relay/environment.json` | 中转机版本和原始源码 SHA-256 |
| `docs/01_程序设计.md` | 模块、数据流、同步、格式和异常恢复设计 |
| `docs/02_部署方式.md` | 现机使用、新机器部署、依赖安装和路径约束 |
| `docs/03_使用方法.md` | 录制、补传、检查、导出和排错命令 |
| `scripts/install_relay_env.sh` | 从包内 wheels 新建导出环境 |
| `validation/` | 本迁移包的测试结果与完整性检查记录 |
| `SHA256SUMS` | 普通文件校验清单；符号链接另见 `SYMLINKS.json` |

已有 Piper 和 D435i 驱动的 GOS，请先阅读 [04_已有驱动环境部署](docs/04_existing_drivers.md)，按依赖检查、只复制程序、复用运行库、启动验收的流程操作。

建议先读部署文档，再按使用文档操作。当前机器可以继续使用原来的命令和环境。

该包不包含 raw/LeRobot 数据集、GOS spool、SSH 私钥、known_hosts、密码或当前日志。数据仍保存在原目录。Python 虚拟环境不直接复制，通过离线 wheels 重建，避免绝对路径失效。

## 完整性校验

在本目录执行：

```bash
sha256sum -c SHA256SUMS
```

本包适配 GOS Ubuntu 20.04 / ARM64 / Python 3.8，以及中转机 Ubuntu 22.04 / x86_64 / Python 3.10。它是应用及部分运行依赖快照，不是系统镜像；新系统仍需安装文档列出的系统软件包。请随包保留第三方依赖自带的许可证和版本信息。

