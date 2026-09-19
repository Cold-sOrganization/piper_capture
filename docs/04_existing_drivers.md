# GOS 已配置好 Piper 和 D435i 时，部署 piper_capture

此流程适用于已经能正常使用 Piper 和 D435i 的 GOS，只增加采集程序，复用现有驱动和运行库。无需重新配置主从模式，无需重装 ROS、Piper SDK 或相机固件。当前这台 GOS 已部署好 piper_capture，可以直接跳到第 4 步查看状态。

## 1. 确认现有环境与采集程序兼容

“已有驱动”不一定等于采集程序所需的 Python 模块和访问方式已就绪。本版本直接读取 USB-CAN 和 RealSense C SDK，不订阅 ROS topic，也不调用 Piper SDK 控制接口。

要求如下：

- GOS 是 ARM64 Ubuntu 20.04，当前已测系统 Python 为 `/usr/bin/python3` 3.8。
- USB-CAN 是兼容 gs_usb 的 `1d50:606f` 适配器，且只接入一个兼容适配器；主从臂共用该 CAN 总线，速率为 1 Mbit/s。仅有 can0 正常工作不能证明当前 PyUSB 采集方式可用；若现有系统采用其他 CAN 适配器或访问后端，需适配采集代码，不要直接改动现有控制链路。
- `/usr/bin/python3` 可导入 numpy、cv2、usb 和 gs_usb。仅在 ROS 或其他虚拟环境中装好模块不够，因为 start_gos.sh 使用系统 Python。
- 现有 ARM64 librealsense C 动态库可加载，支持本程序使用的 API。当前已验证 `/opt/realsense-rsusb-2.51.1/lib/librealsense2.so`，版本 2.51.1。只安装 pyrealsense2 并不一定提供这个路径。
- 当前用户已有 USB-CAN 和 D435i 的访问权限。已有规则有效时不重复覆盖规则。

在 GOS 执行以下检查。它们只导入模块、加载动态库，不打开相机数据流或 USB-CAN 会话：

```bash
PYTHONPATH=/home/user/agx_arm_ws/vendor-python /usr/bin/python3 - <<'PY'
import sys
import numpy, cv2, usb
from gs_usb.gs_usb import GsUsb
from gs_usb.constants import GS_CAN_MODE_LISTEN_ONLY, GS_CAN_MODE_HW_TIMESTAMP
import ctypes
lib = ctypes.CDLL('/opt/realsense-rsusb-2.51.1/lib/librealsense2.so')
lib.rs2_get_api_version.restype = ctypes.c_int
print('Python:', sys.version)
print('numpy:', numpy.__version__, 'OpenCV:', cv2.__version__, 'PyUSB:', usb.__version__)
print('RealSense API:', lib.rs2_get_api_version())
print('Dependencies OK')
PY
```

当前机预期 RealSense API 为 25101。其他版本成功加载仅说明动态链接可用，采集 API 和数据流仍需实际启动验证。

## 2. 只复制 GOS 采集程序

在中转机执行以下命令。先复制到独立暂存目录，再在 GOS 未部署时复制到正式目录：

```bash
cd /home/ccic/piper_migration_20260918
scp -r gos/program user@10.21.31.104:/home/user/piper_capture_incoming
ssh user@10.21.31.104
```

以下在 GOS 执行，要求 `/home/user/piper_capture_incoming` 是刚收到的程序目录，且不存在同名旧暂存目录导致的嵌套：

```bash
test -f /home/user/piper_capture_incoming/collector.py || exit 1
if [ -e /home/user/piper_capture ]; then
  echo 'piper_capture 已存在，请使用现有程序；本步骤不覆盖它。'
else
  cp -a /home/user/piper_capture_incoming /home/user/piper_capture
fi
```

正式目录中只需这 5 个文件：

```text
/home/user/piper_capture/
  collector.py
  control.py
  core.py
  realsense_c.py
  start_gos.sh
```

spool 和运行日志会在使用过程中创建。中转机的 receiver.py、export_lerobot.py、离线 wheels 和导出环境不需要复制到 GOS。

## 3. 仅在依赖路径不同时调整启动脚本

当前机使用默认路径即可，不需要修改。如果其他 GOS 已有依赖位于别处，在新部署的 `/home/user/piper_capture/start_gos.sh` 中设置实际路径，例如：

```bash
export PYTHONPATH="/实际的/vendor-python:${PYTHONPATH:-}"
export REALSENSE_LIBRARY="/实际的/librealsense2.so"
```

放在 `exec /usr/bin/python3 -u collector.py "$@"` 之前。持久写入启动脚本，才能让中转机随后通过 SSH 自动启动时也使用这些路径；只在当前终端 export 不会自动传递给以后的 SSH 会话。

如果检查只缺 usb/gs_usb 等模块，可从包内 `gos/runtime/vendor-python` 复制到一个新的独立目录，再让 PYTHONPATH 指向它；不必替换整个 Piper 驱动环境。若只缺 numpy 或 cv2，应为系统 Python 补齐对应系统包。只有现有库确实不兼容或不存在时，才考虑包内 ARM64 RealSense 库；不同架构不能使用该库。完整依赖部署参考《02_部署方式.md》。

沿用 `/home/user/piper_capture` 及默认 spool 路径：receiver.py 的远程程序和缓存路径已固定。仅给 collector.py 改 --spool，而不修改接收端，会导致传输读取错误。

## 4. 查看或启动采集服务

在 GOS 查看现有服务：

```bash
printf '{"op":"status"}' | /usr/bin/python3 /home/user/piper_capture/control.py
```

若返回 `ok: true`，服务已运行，不需要再次启动。如果明确报 Connection refused，说明本机控制端口当前没有服务监听，再启动：

```bash
cd /home/user/piper_capture
nohup bash start_gos.sh > collector.log 2>&1 </dev/null &
```

等待相机预热数秒，再运行 status。其他错误先看 collector.log，不要不断重启。启动前应确认没有其他程序占用同一相机或 USB-CAN。主从跟随如果依赖另一个软件进程，不要盲目停止该进程，应先确认设备占用能否共存；本采集程序不会接管或重建主从控制链路。

启动后即使没有录制，服务也会保持 CAN 和相机会话。正常录制结束不会退出服务。重启后若出现 `No CAN data for 5s`，当前适配器可能需要物理插拔 USB-CAN，服务会自动检测重连。

## 5. 从中转机录制与验收

已有中转机部署时直接执行：

```bash
cd /home/ccic/piper_capture
/usr/bin/python3 receiver.py status
bash record.sh --task '部署验收：主从臂小幅示教' --duration 10
```

也可在 `/home/ccic/piper_migration_20260918/relay/program` 执行同样命令。输入 GOS 密码，看到 RECORDING 后在正常示教范围内操作，结束后标记结果并等待 SAVED。

验收要点：status 的 errors 为空、相机帧数持续增加、CAN 和相机数据年龄没有持续增大；有效示教时存在主臂 0x155/0x156/0x157/0x159 和从臂 0x2A5～0x2A8。静止或退出有效示教时 action_stale 可能出现，不能仅凭空闲时这一项认定部署失败。用 export_lerobot.py --inspect 检查刚录制的数据是否存在有效连续段。

LeRobot 转换和日常使用见《03_使用方法.md》。本部署流程保留已有驱动配置，采集器只监听机械臂报文；实际同步和有效率仍须以录制结果为准。
