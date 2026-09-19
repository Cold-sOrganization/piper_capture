# Piper 主从示教＋D435i 数据采集

GOS 只接收主从 CAN 报文并采集相机，中转机通过 SSH 拉取校验后的数据块，使用官方 LeRobot 0.4.4 写入 LeRobotDataset v3.0。原始图像采用无损 PNG；深度保留 uint16 和实测尺度。相机为 640×480、15 FPS 彩色＋深度。

## 已部署位置

- GOS：`/home/user/piper_capture`
- 中转机程序：`/home/ccic/piper_capture`
- 中转机原始数据：`/home/ccic/piper_datasets/raw/<episode_id>`
- 中转机导出环境：`/home/ccic/piper_capture/.venv`
- GOS 缓存：`/home/user/piper_capture/spool`，默认上限 2 GiB。

本地源代码：`D:\learn\具身智能学习\piper_capture`。`dev_remote.py` 仅用于开发部署，不属于日常操作，密码只保存在运行进程内存。

## 日常操作（在中转机终端执行）

```bash
cd /home/ccic/piper_capture
python3 receiver.py status
```

输入 GOS 的 SSH 密码。若服务尚未运行，接收端自动启动；相机需预热数秒。正常状态要求 `errors` 为空，`can_age_ms`、`camera_age_ms`较小。主臂四类指令应出现在 `can_counts`：`0x155`、`0x156`、`0x157`、`0x159`。从臂反馈为 `0x2A5`～`0x2A8`。

主臂只在示教有效时发送指令的情况下，静止或退出示教后 `action_stale` 是如实报告。正式开始前，先确认程序已观察到主臂与从臂的七维数据。

开始一次示教：

```bash
bash record.sh --task '将方块放入盒中'
```

看到 `RECORDING` 后开始示教。按 Ctrl+C 结束，随后回答是否成功。结束录制不会改变机械臂状态，程序会继续完成传输。也可以定时录制：

```bash
bash record.sh --task '将方块放入盒中' --duration 30 --success yes
```

`--success yes` 表示预先将本次标为成功，应仅在任务成功确定时使用；否则省略并在结束后标记。`--diagnostic` 允许不完整 CAN 数据，仅用于诊断，导出器始终排除此类 episode。

补传尚未接收完整的数据：

```bash
python3 receiver.py sync
```

程序使用已有 SSH 主机密钥验证连接，不写入密码。可以配置 SSH 密钥后使用 `python3 receiver.py --key-auth ...`。

## 检查和导出

将下面的 `EPISODE_ID` 替换为 `SAVED` 输出中的目录名。GOS 系统日期未校正，所以目录前缀可能是 2024；对齐使用单调时钟，目录另有随机后缀，不依赖日历日期。

```bash
.venv/bin/python export_lerobot.py --inspect /home/ccic/piper_datasets/raw/EPISODE_ID
.venv/bin/python review.py /home/ccic/piper_datasets/raw/EPISODE_ID --output /home/ccic/piper_datasets/review.html
```

HTML 文件可直接用浏览器打开，包含图像抽查、六轴实际/目标曲线和质量统计。

导出一个或多个已经结束的 episode（输出目录必须尚不存在）：

```bash
HF_HUB_OFFLINE=1 .venv/bin/python export_lerobot.py \
  /home/ccic/piper_datasets/raw/EPISODE_ID \
  --output /home/ccic/piper_datasets/lerobot/piper_task_001 \
  --repo-id local/piper_task_001
```

导出只选择成功完成、非诊断、未丢弃的 episode。无效样本、时间间断、异常帧率被分割，不把间断前后拼成连续动作；默认仅导出至少 15 帧的有效连续段。`--include-failed` 可纳入标记失败但正常结束的轨迹，不会放行无效样本。

使用官方加载器：

```python
from lerobot.datasets.lerobot_dataset import LeRobotDataset
ds = LeRobotDataset(
    repo_id="local/piper_task_001",
    root="/home/ccic/piper_datasets/lerobot/piper_task_001",
    video_backend="pyav",
)
sample = ds[0]
print(sample["observation.state"], sample["action"])
```

数据默认只保存在本地，不上传 Hugging Face。

## 字段及同步约定

- `observation.state`：从臂六轴实际角度（rad）＋夹爪实际宽度（m）。
- `action`：CAN 主臂六轴目标（rad）＋夹爪宽度目标（m），与实际反馈独立解码。
- `observation.images.front`：RGB；LeRobot 导出时以 H.264 编码，原始无损 PNG 仍保存在 raw 数据。
- `source.monotonic_ns`：相机映射到 GOS 单调时钟的时间；`source.frame_index`：原始帧索引。
- `sync.color_depth_ms`：同一相机时钟域内的彩色/深度时间差。
- `attachments/depth/episode_*/`：原始 uint16 深度 PNG、相机内外参、`depth_scale_m`、每帧来源和原始时间戳。深度未预先对齐到彩色，也不是伪彩色视频。
- `attachments/capture_manifest.json`：原始 episode 与训练 episode 的映射、字段语义、单位。
- 原始 tar 数据块内还有 `can.jsonl.gz`：全部接收的 CAN 帧、CAN ID、原始 payload、GOS 接收时间和适配器硬件时间。

相机关闭 SDK 的全局墙钟拟合，使用硬件时间，预热期间以低延迟到达样本估计与 GOS 单调时钟的偏移；录制期间固定映射。同步属于软件估计，含相机传输、CAN USB 接收延迟和时钟漂移，不能声称硬件级同步或绝对毫秒精度。每帧保留全部原始时间，供后续校准。

第一版采用相机时刻之前最后一组 CAN 指令/反馈，关节组三包时间跨度不超过 20 ms，各分量年龄不超过 100 ms；彩色/深度差不超过 35 ms。阈值可在 GOS 启动参数中配置。动作不是未来从臂反馈，也不会用零值补缺失。SDK 单帧缓存未更新时，不会算成新消息。

经典 CAN 无发送者地址。主从来源按已配置且不冲突的指令/反馈 ID 区分，必须保持现有正确的主从配置。夹爪反馈为角度模式时不能冒充米制宽度，程序将该样本判无效。

## 传输、恢复与资源限制

- 数据约每 2 秒封装为一个原子提交的数据块，包含图像、同步样本和原始 CAN。
- 中转机支持 `.part` 断点续传，核对大小和 SHA-256，fsync 后才确认；默认收到确认后释放 GOS 副本。`--keep-gos` 可保留。
- 网络传输可以落后于采集，GOS 短期缓存吸收积压；缓存达到 2 GiB 或磁盘低于 512 MiB 会停止本次录制并标记失败。持续录制能力需要以实际网络吞吐为准。
- 有界事件/图像队列溢出会明确标记失败，不静默丢帧。进程重启时把未完成 episode 标记为 `interrupted`。
- 常驻采集服务使多次 episode 共享同一个 CAN 会话；退出接收端不会关闭 CAN。
- 当前适配器已复现 PyUSB 关闭后再次打开收不到数据。重启服务后如出现 `No CAN data for 5s`，需物理插拔 USB-CAN；服务检测新的 USB 设备编号后自动恢复接收。相机无需插拔。
- 采集器使用 CAN 硬件 listen-only，不发送机械臂控制帧，不设置主从模式、不使能、不归零。不要同时启动其他占用同一个 USB-CAN 的 Piper 程序。

## 测试

```bash
cd /home/ccic/piper_capture
HF_HUB_OFFLINE=1 .venv/bin/python -m unittest discover -s tests -v
```

协议测试覆盖单位、符号、缺失动作、过期数据、未来数据、关节报文不同步、时间间断。数据集测试使用临时且明确标记为 `SYNTHETIC_TEST_ONLY` 的数据，验证官方写入/加载、视频解码、动作时间窗口、深度无损和损坏文件拒绝。此测试不能替代真实示教验收。
