---
name: device-io
description: 统一 USB/串口设备枚举、物理板卡分组、端口角色解析（log/burn/control 等）、驱动适配器识别，以及 Windows 幽灵（已拔出但未卸载）设备实例的预览与清理。只读优先，唯一的写操作默认 dry-run。
---

# device-io

一个跨 Windows / Linux 的设备视图层。**它不烧录、不开串口、不改节点配置**，只回答三个问题：

1. 现在插着哪些板卡，每块板卡上有哪些串口和音频端点？
2. 哪个端口承担哪个角色（日志口 / 烧录口 / 控制口 / 提示音口）？
3. 需要的驱动在不在？（Windows 还多一个：有没有幽灵设备实例在捣乱？）

## 何时使用

- 烧录/测试流程开始前的 preflight：不要再用 `COM3` 这种硬编码端口名判断，用角色解析。
- 现场排查"板子插着但端口不见了"：先看树，再看驱动，最后看幽灵实例。
- 两个同型号板卡同时插着、需要区分：用 `board_key` + 角色约束，不要靠端口出现顺序。

## 何时不要使用

- 需要真正烧录固件：用 `venusahisiburner`，它调用本仓库拿端口。
- 需要 GUI 给人看：`usb-tree` 的图形界面仍可用，本仓库的 `--format tree` 只是文本版。
- 需要跑语音测试：用 `lstest`，它同样调用本仓库。

## 快速开始

```bash
# 看树（Windows / Linux 同一套分组规则）
device-io ports

# 机器可读
device-io ports --format json
device-io boards --format json

# 角色解析（推荐给所有自动化流程）
device-io roles --spec roles.json
device-io roles --spec roles.json --format json --save out/role-map.json

# 驱动体检
device-io driver check --spec roles.json

# Windows 幽灵实例：先预览，确认后才清理
device-io clearusb
device-io clearusb --apply
device-io clearusb --apply --board 8765:5678

# 自检（后端可用性、PowerShell、版本）
device-io doctor
```

退出码：`0` 成功；`1` 参数/契约错误；`2` 角色解析失败（硬件在但映射不对）；`3` 当前平台不支持该操作；`4` 后端不可用（例如在 Linux 上要 Windows 采集器）。

## 角色定义（两个状态都不是"猜"，要么确定要么报错）

```json
{
  "schema": "ark.device-roles-spec.v1",
  "strict": true,
  "board": {"vid": "1a86", "pid": "55d3"},
  "roles": [
    {"role": "burn", "interface": "SERIAL-A", "driver": "ch34x", "required": true},
    {"role": "log",  "interface": "SERIAL-B", "required": true},
    {"role": "control", "pin": "COM6", "required": false}
  ]
}
```

结果只有五种：`resolved` / `pinned` / `ambiguous` / `missing` / `conflict`。**永远不会**"取第一个匹配的端口"——`ambiguous` 会停下来要求收紧条件，`conflict` 表示同一个端口被两个角色认领。`ok` 为 false 时不要继续烧录。

## 时间与字段约定

- 所有文档带 `schema` 字段，形如 `ark.device-io.v1`；只增字段，不删不改名。
- `generated_at` 统一格式 `YYYY-MM-DD HH:MM:SS`，北京时间，字符串里**不带时区**；禁止 `T`、`Z`、`+08:00` 和 RFC3339（例：`2026-11-10 20:47:15`）。不要当 UTC 解析。
- 端口标识 `port.id` = `board_key:设备名`，比裸端口名稳定；跨机器比较时用 `board_key` + `interface`。
- 只有 `raw_` 开头的字段可以保留外部原始时间格式（原样透传、不参与解析）；其他字段出现异形时间即违约。`tools/time_lint.py` 会在门禁里扫描全仓。

## 平台差异（保留差异，不装作一样）

| 维度 | Windows | Linux |
|---|---|---|
| 枚举 | PowerShell CIM | 只读 sysfs |
| 板卡身份 | Container ID / PnP 父实例 | 上游 USB 设备路径 |
| 接口标签 | `MI_XX` → `SERIAL-A..D` / `Ch A..H` / `VCOM` | `:1.N` 后缀 |
| 驱动证据 | 驱动服务名 + 设备状态 | `driver` 符号链接（内核模块） |
| 幽灵实例 | 有，可清理 | 概念不存在，明确拒绝而不是假装清理 |

## 边界

- 唯一的写操作是 `clearusb --apply`，仅 Windows、仅非在位设备、默认预览。
- 不安装驱动、不改注册表、不重启、不动 `sshd_config`、不改节点环境变量。
- 不执行任何烧录或串口读写。
- 第三方板卡通过 `--plugin` 加载 `ark.device-adapter.v1` JSON 扩展，不要改本仓库源码。
- 详细字段表、Python API、迁移方法与退出码语义见 `docs/interface.md` 与 `MIGRATION.md`。
