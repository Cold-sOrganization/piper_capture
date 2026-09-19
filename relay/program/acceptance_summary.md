# 2026-09-18 采集程序验收

源代码和操作说明：[README.md](README.md)。

- GOS 采集器已部署，使用一个硬件 listen-only CAN 会话；相机复用已安装 librealsense 2.51.1。
- 中转机已离线安装 LeRobot 0.4.4 的数据集环境；原始数据、LeRobot 数据集均保存在中转机本地。
- 8 项自动测试全部通过。官方 LeRobot 加载器已读取真实数据的各片段边界、RGB 图像、七维状态/动作和三步动作窗口。
- 20 秒手动示教实测：294 组原始样本，229 组满足有效性检查；剔除短段后导出 6 个连续片段，共 210 帧。
- 原始 CAN 帧：49,775；每条保留 payload、CAN ID 和时间戳。
- 彩色与深度硬件时间差最大约 0.121 ms。这不是机械臂与相机的绝对同步误差；机械臂对齐使用 USB 接收时刻和软件时钟映射。
- 录制中最大图像间隔为 467.04 ms；另有过期动作和关节分包间隔过大。导出已分段/剔除，原始记录完整保留，不能宣称全程无丢帧。
- 从预留 1 MiB 部分文件恢复接收，剩余 131,784,704 字节耗时约 50.54 秒；全部 SHA-256 校验通过。第二次同步下载 0 字节。
- 网络传输慢于这次无损采集的数据产生速度。当前策略是边采边传、GOS 缓存积压、录制后补传；长时间连续采集尚未完成压力验收。
- 第一帧可见夹爪前端、地面和操作人员脚部。正式任务采集前，应按具体任务确认操作物体和工作区域在画面内。

中转机文件：

```text
/home/ccic/piper_datasets/raw/20240802_042025_2724320b
/home/ccic/piper_datasets/lerobot/acceptance_2724320b
/home/ccic/piper_datasets/acceptance_report.json
/home/ccic/piper_datasets/acceptance_review.html
```

本地也保存了 `acceptance_report.json`、`acceptance_review.html` 和图像抽查 `acceptance_color.png`。

验收数据仅用于确认采集和格式链路；不等同于完成了一个具体操作任务的数据集。

