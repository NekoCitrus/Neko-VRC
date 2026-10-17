# Neko-VRC
[基本设置](assets/screenshots/general.png) · [郊狼设置](assets/screenshots/coyote.png) · [负鼠设置](assets/screenshots/opossum.png) · [运行调试](assets/screenshots/debug.png) · [版权信息](assets/screenshots/about.png)

[English version](README_en.md)

当前版本：**v2.0.0**。源码默认前端已改为 WinUI 3；包含电脑直连蓝牙、独立四通道触发、OSCQuery 参数读取与回退提示，以及精简的多设备 chatbox 显示。上方截图为旧版界面。

一个通过 VRChat OSC 读取 Avatar SPS/OGB 插入深度，并联动郊狼 DG-LAB 3.0 或负鼠振动控制器的小工具。

我们的 VRChat 群组： [ShockingVRC https://vrc.group/SHOCK.2911](https://vrc.group/SHOCK.2911)

> [!CAUTION]
> 您必须阅读并同意 [安全须知](doc/dglab/安全须知.md) ([Safety Precautions](doc/dglab/SafetyPrecautions.md) in English) 后才可以使用本工具！

## 使用方式

1. 前往 [本项目 Release](https://github.com/NekoCitrus/Neko-VRC/releases) 下载最新版本的 Neko-VRC
2. 完整解压发布包，运行 `Neko-VRC.exe`。WinUI 3 界面跟随 Windows 浅色 / 深色主题，自动启动后台服务；无需另装 Python、.NET 或 Windows App Runtime。Windows 11 使用系统窗口圆角，Windows 10 外观随系统能力降级。
3. 在“郊狼设置”和“负鼠设置”中勾选各通道的触发来源，并设置部位、参数、波形和强度上限。
4. 设置修改后自动应用并保存，无需点击保存；连续拖动会约每 120 毫秒合并更新一次。已有 APP 连接不会断开。首次联网时如弹出 Windows 防火墙提示，请选择允许。
5. 每类设备分别选择 **App** 或 **蓝牙直连**，新配置默认蓝牙直连，已保存的连接方式不会被覆盖。App 模式使用 DG-LAB 4 APP 的 Socket 扫码；直连模式点击“扫描蓝牙”、选择主机，再点击“连接”。连接前请释放手机对该主机的蓝牙连接。
6. 如勾选“关闭主窗口后继续在系统托盘运行”，关闭窗口不会停止服务；可从托盘菜单重新打开或退出。

## 桌面窗口

- **基本设置**：浅色 / 深色 / 跟随系统（切换即生效并自动保存，无需点击保存）、Chatbox、后台运行与 SteamVR 跟随启动。程序通过 OSCQuery 供 VRChat 自动发现，无需额外分流软件。
- **郊狼设置**：分别设置 A/B 的插座深度（Socket）、插头深度（Plug）、额外参数、波形和强度上限。首项“纯粹”为新配置默认值，不叠加预设波形起伏，持续按当前计算比例输出；保留设备所需脉冲格式，并受原有上限限制。已有波形选择不变。
- **负鼠设置**：使用独立 A/B 设置和负鼠官方波形。
- **运行调试**：只显示郊狼/负鼠各通道的计算强度与设备回报强度，数值不变不重复打印，新记录自动滚动到底部。未取得设备回报时显示“未反馈”。连接状态与电量在各自设置页查看。
- **版权信息**：显示项目、代码来源、前端贡献者与开源许可。

程序当前接受一个 DG-LAB APP 连接，但会同时控制该 APP 上报的所有受支持郊狼与负鼠槽位。

每个通道的触发项分为深度与部位、PCS/LMS 回退、参数/地图事件三列。强度上限使用无刻度和端点数字的 0～200 整数滑块，与输入框双向同步；原生滑块轨道内的加粗蓝色电平显示当前计算强度，A/B 标题右侧显示触发来源及计算值。这不是波形瞬时幅值或硬件反馈。多个同类主机显示各自有效上限计算值中的最大值；暂停或未连接时电平为 0。

所有可编辑设置自动应用和保存。输入暂时为空或无效时保留上次有效配置，并显示错误，修正后自动继续。改上限/波形保留当前深度，改触发项只更新相关输入；主题不重载设备。手动修改 YAML 仍需重开软件。

郊狼和负鼠可以分别选择不同的连接方式；同一类设备只向选中的连接路径输出。蓝牙模式每类设备连接一台主机，App 模式仍支持多槽位。保存通道设置不会断开现有连接；改变连接方式或蓝牙地址时，会先清除旧路径输出并断开旧蓝牙设备，需要再手动连接。

“暂停”停止所有输出，点击“恢复”后重新接收触发。首次连接、重连和输入过期时从零输出开始，不恢复旧波形。断线情况下无法保证归零指令到达硬件，请使用主机实体停止按钮。

窗口默认尺寸为 940 × 640，最小尺寸为 880 × 640（随 DPI 缩放）。标题右侧仅保留“暂停 / 恢复”，服务状态、参数提示与 Avatar 名称位于按钮同一行左侧，长名称省略并可悬停查看。主窗口不再提供保存或重复的退出按钮。扫码、扫描、连接、断开仍在原位置。标题区与设置卡片保持紧凑，保留原生切页和蓝条动画。关闭退出时立即隐藏窗口，在后台保存有效设置并完成清理；启用托盘运行时关闭仍只进入托盘。

### 跟随 SteamVR 启动

“基本设置”中的“跟随 SteamVR 启动”复选框通过 OpenVR 应用清单启用或关闭 SteamVR 自动启动。更改该选项前请先启动 SteamVR，勾选后自动应用。清单保存在 `%APPDATA%\Neko-VRC\steamvr\`；程序启动时会重新校验并修复 exe 移动后的路径。

该功能不会使用 Windows 开机启动项，也不会自行启动或轮询 SteamVR。SteamVR 首次读取新清单时可能要求重启 SteamVR；窗口会显示相应提示，并在下一次启动时继续完成设置。

## 配置文件

配置固定保存在：

```text
%APPDATA%\Neko-VRC\settings-v2.0.yaml
```

每次启动优先在 `Neko-VRC.exe` 同级的 `log\` 创建独立时间戳日志（源码运行则在项目的 `log\`；单文件 20 MB 轮转，不覆盖旧运行记录）。目录不可写时回退到 `%APPDATA%\Neko-VRC\log\`。主程序日志名为 `neko-vrc`，文件保留连接错误与诊断信息，强度不变时不再五秒重复打印；调试页仅显示强度。文件日志至少保留 INFO，包含四通道计算值与真实回报值、比例、来源、上限与可测耗时；发送目标不会冒充设备回报。延迟包含 OSC 计算、发送/入队、App 确认、OSCQuery HTTP、BLE 连接/写入和配置应用，不代表端到端动作延迟。新目录无配置时从旧 `%APPDATA%\ShockingVRChat` 只读迁移，配置仍只向 Neko-VRC 目录写入。打包不包含运行中产生的用户日志。

服务运行时，每 5 秒通过 OSCQuery 查询 VRChat 实际 OSC 接收地址并发送布尔值 `/avatar/parameters/OGB_ENABLED=true`，不依赖 Chatbox 或设备连接。退出时停止发送，但不发送 false，避免干扰其他兼容软件。

连接栏直接显示每条连接的通信耗时：App V4 优先显示最近 30 秒内的请求确认往返，否则使用 WebSocket Ping/Pong 往返（V3 也适用）；蓝牙显示 GATT 写入耗时，不支持写入确认时明确标为“蓝牙入队”。未取得样本时显示“暂无数据”。这些数值不包含 App 到主机的蓝牙链路或设备动作耗时。

郊狼 3.0 直连先订阅通知，再归零及写入软上限；需要确认强度时使用非零序列号请求 B1 回报，等待确认期间继续发送波形，暂停/归零不被等待阻挡。SPS、PCS 与地图触发无需手机 App；旧 `sendwave` 接口也经过同一输出路由。

## 设备控制方式

蓝牙直连支持郊狼 2.0、郊狼 3.0 和负鼠 V1，各自使用独立协议编码。3.0 在连接及上限更新时写入 BF 软上限；负鼠分别发送 B3 通道强度与 B0 振动波形。软件上限保持固定，触发比例只调制波形幅值。2.0 每 100ms 仅支持一组 XYZ，因此对 3.0 波形库做帧内平均近似，体感不保证相同。负鼠屏幕 B2 指令超过文档所列 20 字节，协商 MTU 不足时跳过屏幕同步，不拆分该指令。

协议与模拟测试已覆盖；实际蓝牙兼容性、波形体感和断连行为仍需对应主机验证。官方参考：[郊狼 2.0](https://github.com/dungeonlab-open/dglab-bluetooth-protocol/blob/main/coyote/v2/README.md)、[郊狼 3.0](https://github.com/dungeonlab-open/dglab-bluetooth-protocol/blob/main/coyote/v3/README.md)、[负鼠](https://github.com/dungeonlab-open/dglab-bluetooth-protocol/blob/main/opossum-vibrate-controller/README.md)。

App 模式下，郊狼与负鼠可经同一条 DG-LAB 4 APP Socket V4 连接控制，APP 将每台主机作为独立 `slotId` 上报。程序保留所有受支持槽位，按设备连接方式路由任务；负鼠波形转为 OVC 固定前缀与四个振动强度字节。参见 [DG-LAB 官方 dglab-kit](https://github.com/dungeonlab-open/dglab-kit)。

### 源码构建

在 Windows 上准备 .NET 10 SDK、Windows SDK（10.0.26100.0）与 Python 环境，安装 `requirements-build.txt` 后运行：

```powershell
powershell -ExecutionPolicy Bypass -File build-winui.ps1
```

输出目录为 `products/v2.0.0/Neko-VRC-WinUI-win-x64/`，压缩包为 `products/v2.0.0/Neko-VRC-WinUI-v2.0.0-win-x64.zip`。成品按版本保存在 `products/`，后台编译中间文件保存在 `build/`；原 `dist/` 保留历史产物，后续标准构建不再往里面写入。这是免安装 x64 包，必须完整分发，不能只复制前端 exe。

源码调试：

```powershell
.tools\dotnet\dotnet.exe build desktop/NekoVRC.WinUI/NekoVRC.WinUI.csproj -p:Platform=x64
.venv\Scripts\python.exe neko_vrc.py
```

WinUI 3 使用原生 `NavigationView`（选中指示条动画）、`TitleBar` 和 Toolkit 设置卡片；布局参考 [WSL Settings](https://github.com/microsoft/WSL/tree/master/src/windows/wslsettings) 与 [WinUI Gallery](https://github.com/microsoft/WinUI-Gallery)。导航与内容共用背景，窗口按钮由 Windows 绘制，额外参数默认折叠。第三方许可见 [THIRD-PARTY-NOTICES.txt](THIRD-PARTY-NOTICES.txt)。

原 WPF 工程保留：`build-wpf.ps1` 构建旧前端；源码启动加 `--wpf-ui` 显式选用已构建的 WPF 前端。`--legacy-ui` 选用更早的 Win32 前端。新构建不会覆盖已有 WPF 发布目录。

一般设置，以及郊狼/负鼠各自的 A/B 触发、部位、波形和强度上限，建议直接在窗口修改。WebSocket 或 Web 服务端口等高级选项仍可在 YAML 中修改；手动修改 YAML 后请从托盘退出程序并重新打开。

## 强度计算

每个设备的每个通道有五个独立触发复选框；只从勾选的来源中取最大值：

- **SPS Socket 被插入深度**：读取 `OGB/Orf/<部位>` 的 Root/Tip 数据，按 OGB 算法估算插入深度。
- **SPS Plug 插入深度**：读取 `OGB/Pen/<部位>` 的 `PenSelf/PenOthers` 深度。
- **额外参数**：按旧 distance 算法将数值从默认 `0～1` 范围映射为深度；0.5 秒未更新即失效。
- **监听 PCS**：无有效 Socket 深度参数时，使用 `/avatar/parameters/pcs/contact/proximity` 按 distance 计算。
- **监听 LMS**：所选 Plug 深度为 0 时，使用 `/avatar/parameters/lms/contact/proximity` 按 distance 计算。

| 触发来源 | 郊狼 A/B 默认 | 负鼠 A/B 默认 |
|---|---|---|
| Socket、Plug、参数 / 地图事件 | 开 | 开 |
| 监听 PCS | 开 | 关 |
| 监听 LMS | 关 | 开 |

Socket 和 Plug 可分别选择指定部位或“任何当前正在触发的”部位。程序通过 OSCQuery 读取当前 Avatar 和 SPS 部位；`/avatar/change` 仅用于要求立即刷新。

仅可读的数值参数参与可用性判断。部位列表及数量仅包含可计算深度的插座/插头：完整同部位同来源 Root/Tip 对或直接深度，不计 Touch/Frot、半对和不可读信号。Socket 回退判定以实际读到的有效深度信号为准，零值也有效；仅声明了参数不会阻止 PCS 回退。OSCQuery 当前值用于补充实时 OSC，超过 0.5 秒失效，暂停时不用于恢复输出。

参数提示统一放在顶部服务状态右侧，不再在每个通道重复显示。尚未发现模型时提示“等待读取模型 OSC 参数”；缺少深度且存在对应回退参数时推荐使用 PCS/LMS；深度和回退均不存在时提示“无任何可用插座/插头参数”。提示不会自动修改复选框。PCS、LMS 和参数/地图事件后的问号可悬停查看说明。

游戏内关闭 LMS、LMS 归零或超时不会清除其他仍有效的 SPS 深度；参数目录变化仅移除消失的信号。cheese 地图事件使用不叠加预设起伏的纯粹输出，受各设备独立上限限制，事件结束后恢复 SPS 所选波形。

自身 Socket 与自身 Plug 交互同样参与计算，`Self` 与 `Others` 取最大值，无需额外开关。请在 VRChat 中启用 `Avatar Self Interact`，并确保模型输出自身接触的 OGB 深度参数；只有吸附而没有深度信号时不会触发。参见 [OGB 设置说明](https://github.com/OscToys/osc.toys/blob/main/docs/20-getting-started.mdx)。

最终深度为 `0～1`，线性缩放当前设备通道的波形幅值。

勾选“监听 LMS”后，该通道所选 Plug 的 `PenSelf/PenOthers` 深度均为 0 时，LMS 按通道的 distance 上下限（默认 `0～1`）参与计算；任一 Plug 深度大于 0 时忽略 LMS。郊狼也可手动勾选 LMS。

勾选“监听 PCS”后，仅在缺少匹配的有效 Socket 深度参数时才使用 PCS。有效参数指同部位、同来源的完整 Root/Tip 对，或旧版 `PenSelf/PenOthers`，类型需支持数值；深度为 0 不会启用 PCS。判定参考当前 OSCQuery 参数树和实际 Socket 信号，随模型、参数和部位变化更新。

PCS 和 LMS 各自受对应复选框控制，不依赖“参数 / 地图事件”开关，0.5 秒未更新即失效。关闭 Socket / Plug 只关闭其强度贡献，必要的回退状态检测仍会保留。修改触发项仅清理相应输入；修改上限或波形不清空深度，无需重新扫码。


## 配置文件参考

配置文件格式为 YAML，当前版本为 `v2.0`。郊狼和负鼠的 A/B 设置分别位于 `channels.dglab3.coyote` 和 `channels.dglab3.opossum`。

```yaml
version: v2.0
channels:
  version: v2.0
  dglab3:
    coyote:
      channel_a: {socket_zone: '*', plug_zone: '*', strength_limit: 100}
      channel_b: {socket_zone: '*', plug_zone: '*', strength_limit: 100}
    opossum:
      channel_a: {socket_zone: '*', plug_zone: '*', strength_limit: 100}
      channel_b: {socket_zone: '*', plug_zone: '*', strength_limit: 100}
settings:
  version: v2.0
  osc:
    listen_host: 127.0.0.1
    listen_port: 9001
  relay:
    enabled: false
    listen_host: 127.0.0.1
    listen_port: 9001
    vrcft_host: 127.0.0.1
    vrcft_port: 9011
    internal_host: 127.0.0.1
    internal_port: 9021
  chatbox:
    enable: true
```

## 模型参数配置

- Avatar 必须包含兼容的 SPS/OGB 参数。
- Socket 参数路径应以 `/avatar/parameters/OGB/Orf/` 开头。
- Plug 参数路径应以 `/avatar/parameters/OGB/Pen/` 开头。
- 程序会从 VRChat OSCQuery 的 `/avatar` 参数树提取部位列表；下拉框中的 `*` 表示“任何当前正在触发的部位”。额外参数框仍接受 OSC 路径和通配符。
- 如果下拉列表为空，请先在 VRChat 中切换到目标 Avatar，并确认 VRChat OSC 已启用且模型公开了兼容的 SPS/OGB 参数。

## 高级设置参考

以下片段对应配置文件的 `settings` 节点内部：

```yaml
SERVER_IP: null # 为 null 时程序将尝试自动获取本机 IP
dglab3:
  coyote:
    channel_a: # 郊狼通道 A 配置
      depth: {freq_ms: 10, waveform: 呼吸}
    channel_b: # 郊狼通道 B 配置
      depth: {freq_ms: 10, waveform: 呼吸}
  opossum:
    channel_a: # 负鼠通道 A 配置
      depth: {freq_ms: 10, waveform: 呼吸}
    channel_b: # 负鼠通道 B 配置
      depth: {freq_ms: 10, waveform: 呼吸}
general: # 通用配置
  run_in_background: true
  steamvr_auto_start: false # 跟随 SteamVR 启动；建议通过窗口修改
  local_ip_detect:  # 探测本地 IP 时使用的服务器地址
    host: 223.5.5.5 # 默认为 AliDNS 如果在中国大陆以外使用，请适当修改
    port: 80
log_level: INFO # 日志等级，诊断问题时可以改为 DEBUG
osc: # OSC 服务配置
  listen_host: 127.0.0.1 # 如果 VRChat 在其他主机运行，请改为 0.0.0.0，并给 VRChat 正确配置 osc 启动命令行参数。
  listen_port: 9001
version: v0.8 # 配置文件版本
web_server: # Web 服务器配置
  listen_host: 127.0.0.1 # 如果需要从其他主机打开网页扫码，请改为 0.0.0.0
  listen_port: 8800
ws: # Websocket 服务配置
  listen_host: 0.0.0.0
  listen_port: 28846
  master_uuid: 6da2fd3b-a6e5-4af4-afc1-96bfd2e9e95c # 首次启动自动随机生成

```

### Chatbox 与控制接口配置

Chatbox 等待连接时不发送文字；最后一台设备断开后清空显示。已连接设备每台仅显示一行 A/B 有效强度上限，包含 App 和蓝牙直连设备，不附加介绍。

程序会自动补充缺少的配置项：

```yaml
chatbox:
  enable: true
  osc_host: 127.0.0.1
  osc_port: 9000
  update_interval: 3.0
  set_avatar_parameter: true
api:
  control_enabled: false
  token: 自动生成的随机令牌
```

原版地图联动接口 `/api/v1/shock/<A|B|all>/<秒数>` 允许来自本机或局域网 VRChat `UnityPlayer` 的请求触发郊狼和负鼠，不需要启用通用控制 API。各设备 A/B 通道的“参数 / 地图事件”复选框同时启用额外 OSC 参数和地图事件，保存后热更新。地图事件使用各通道的波形和有效强度上限，持续时间结束后恢复当前 Socket、Plug 或额外参数输出。`/api/v1/sendwave/...` 等原始波形接口仍默认关闭；确实需要时才将 `control_enabled` 改为 `true`，并通过查询参数 `?token=...` 或请求头 `X-Control-Token` 提供令牌。请勿公开该令牌，也不要将 Web 服务暴露到公网。

`/api/v1/status` 保持原版地图兼容格式，郊狼排在负鼠之前；只有负鼠连接时也会报告设备在线。其中 `attr.strength.A/B` 为对应设备的软件设置和设备上限共同决定的有效强度上限。

## 开发与打包

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-build.txt
python -m unittest discover -v
powershell -ExecutionPolicy Bypass -File build-winui.ps1
```

脚本构建自带运行时的 WinUI 3 前端及无控制台 Python 后台，生成 `products\v2.0.0\Neko-VRC-WinUI-v2.0.0-win-x64.zip`，并使用隔离配置验证页面绑定、退出按钮和系统关闭。解压后运行完整目录中的 `Neko-VRC.exe`，保留 `backend` 子目录。配置保存在 `%APPDATA%\Neko-VRC\`，不写入发布目录。WPF 回退构建同样输出到 `products\v2.0.0\`，文件名包含 `WPF`。

## FAQ

### 是否有逃生通道

- 郊狼可以按一下任意一侧肩键，此时 A/B 通道强度会被设置为 0。负鼠请使用 APP 停止输出或断开设备。
- 当程序检测到通道强度被用户主动设置为 0 后，将不再自动跟随强度上限。
- 还原需要手动在手机上点击 "+" 键，将通道强度 +1 ，即恢复自动跟随。

### 应该如何设置上限

- 建议通过郊狼 APP 内的被控设置进行调整，程序将跟随。
- 在“郊狼设置”或“负鼠设置”页面调整对应通道上限，修改会自动应用。
- 为保证强度自动跟随自动运行，请确认郊狼APP内 菜单-被控设置 中，两个通道的强度上限初始值（最小值）大于等于 1。

### 想用同一个 SPS 部位同时触发两个通道

- 在对应页面为 A、B 选择相同的 Socket/Plug 部位，修改会自动应用。

### OSC 端口冲突了怎么办

报错包含 `WinError 10048` 时，说明另一个程序已占用本程序的 OSC UDP 端口。请退出占用该端口的程序，或在进阶配置的 `osc.listen_port` 中选择未占用端口，然后重启服务。VRChat 可通过 OSCQuery 自动发现本程序，无需安装额外分流软件。

### 控制台内有波形输出，但是没有强度或强度显著变小

- 确认贴片正常连接，确认电线正常连接
- 试试看按一下按钮将郊狼强度设置为 0 之后，再手动点击屏幕 +1 恢复正常模式。

### 程序看起来收不到 OSC 数据

1. **如果你有面捕**，请检查 Steam 中 VRChat 的启动命令行参数，是否有类似 `--osc=9000:127.0.0.1:9001` 的配置；如需手动指定，其发送端口应与进阶配置的 `osc.listen_port` 一致。
2. Action Menu 中选择 Options > OSC > Reset Config 重置 OSC 配置
3. 如果之前是正常使用的，但忽然收不到，重启电脑可以解决问题，似乎是 VRChat 的 Bug。
4. 目前**已知会占用 UDP 9000 端口导致 VRChat OSC组件启动失败的程序**，请退出以下程序并重置OSC。
    - 酷狗音乐

### 为什么强度一直是最大可用值

- 每台郊狼的通道强度取自身 `intensityMax` 与“郊狼设置”页面对应上限的较小值；负鼠使用“负鼠设置”页面中的独立上限。
- SPS 插入深度 `0～1` 线性缩放波形幅值；“纯粹”将每个采样值设为当前计算比例 × 100，不叠加预设起伏。设备强度上限与该实时比例是独立的。

### APP 扫码无法连接/连接超时

1. 请确认手机和电脑在同一个网络内，例如手机不可以使用流量。
2. 请检查窗口二维码下方的连接地址，例如 `ws://192.168.1.2:28846/?tid=...`，其中 IP 是否为手机可以访问的电脑局域网 IP；不能是 `127.0.0.1`。
3. 如果IP错误，请在进阶配置文件中 `SERVER_IP:` 填写正确的 IP 地址后重启程序再试。
4. 请确认 Windows 防火墙是否允许本程序访问网络（接受传入连接）。
5. 新版二维码采用 DG-LAB 官方 Socket V4 格式；如果状态显示“APP 已连接（V4，等待设备）”，说明网络和扫码已正常，需要在 APP 内通过蓝牙连接郊狼或负鼠。

### 程序版本更新后配置文件如何继承？

- v0.8 首次启动会迁移 v0.7 及更旧配置并保留原文件。共享触发设置会作为两类设备的初始值。

### OSC 能收到其他参数但收不到模型的参数

- 确认模型包含 `/avatar/parameters/OGB/Orf/...` 或 `/avatar/parameters/OGB/Pen/...`。
- 如果模型刚刚修改过，可能是 VRChat 的 OSC 配置文件没有更新，请在 Action Menu 中选择 Options > OSC > Reset Config。

## Credits

感谢 [DG-LAB](https://github.com/dungeonlab-open) 提供设备、开放协议与技术生态。

本程序的原始项目与代码来源为 [Shocking-VRChat](https://github.com/VRChatNext/Shocking-VRChat)，Chatbox 发送部分来源为 [DG-LAB-VRCOSC](https://github.com/ccvrc/DG-LAB-VRCOSC)。

前端界面部分由 WenX1ang、猫橘Citrus 与 ChatGPT 贡献。

感谢以下用户对常见参数部分的协助：ichiAkagi

-----

## 安全须知

**为了您能健康地享受产品带来的乐趣，请在使用前确保已阅读并理解本安全须知的全部内容。**  
**错误使用本产品可能对您或者他人造成伤害，由此产生的责任将由您自行承担。**

感谢您选择DG-LAB系列产品，用户的安全始终是我们的第一要务。  
本产品为情趣用品，请保证在**安全，清醒，自愿**的情况下使用。并将其放置于未成年人接触不到的地方。

本安全须知大约需要**2分钟**阅读。

### **下列人群严禁使用本产品：**

1. **佩戴心脏起搏器，或体内有电子/金属植入物的人群**（可能影响起搏器或植入物的正常功能）
2. **癫痫，哮喘、心脏病、血栓及其他心脑血管疾病患者**（感官刺激可能诱发或加重症状）
3. **皮肤敏感，皮炎及其他皮肤疾病患者**（可能使皮肤疾病症状加重）
4. **有出血倾向性疾病的患者**(电刺激会使局部毛细血管扩张从而可能诱发出血)
5. 未成年人、孕妇、知觉异常及无表达意识能力的人群
6. 肢体运动障碍及其他**无法及时操作产品**的人群（可能在感到不适时无法及时停止输出）
7. 其他正在接受治疗或身体不适的人群。

### **下列部位严禁使用本产品：**

1. 严禁将电极置于胸部；**绝对禁止将两电极分别置于心脏投影区前后、左右**或任何可能使电流流经心脏的位置；
2. 严禁将电极置于**头部、面部，眼部、口腔、颈部**及颈动脉窦附近；
3. 严禁将电极置于**皮肤破损或水肿处，关节扭伤挫伤处，肌肉拉伤处，炎症/感染病灶处，或未完全愈合的伤口**附近。

### **其他注意事项：**

1. **严禁在同一部位连续使用30分钟以上，**长时间使用可能导致局部红肿或知觉减弱等其他损伤。
2. 严禁在输出状态下移动电极，**在移动电极或更换电极时，必须先停止输出，**避免接触面积变化导致刺痛或灼伤。
3. 严禁在驾驶或操作机器等危险情况下使用，**以避免受脉冲影响而失去控制。**
4. 严禁将电极导线插入产品主机导线插孔之外的地方（如电源插座等）。
5. 严禁在具有易燃易爆物质的场合使用。
6. **请勿同时使用多台产品。**
7. 请勿私自拆卸或修理产品主机，可能会引起故障或意料外的输出。
8. 请勿在浴室等潮湿环境使用。
9. 在使用过程中，**请勿使两电极互相接触短路，**可能导致感受减弱，接触部位刺痛或灼伤，或损坏设备。
10. 电极使用时必须与皮肤充分紧密接触，如果电极与皮肤的接触面积过小，可能导致刺痛或灼伤。如果电极与皮肤的接触面积过大，则可能导致电感微弱。
11. 产品内含锂电池，禁止拆解，装机，挤压或投入火中。若产品出现**故障或异常发热**，请勿继续使用。

### **重要使用提示：**

1. 由于不同部位对于电流耐受程度存在差异，且一些材质的电极可能使少部分用户出现过敏现象。**当您在一个部位首次使用本产品时，或使用一款新的电极时，请先试用10分钟**之后等待一段时间，确认使用部位无异常后方可继续使用。
2. 受人体生理特性的影响，身体对于脉冲刺激的感受会逐渐变弱，因此，在使用过程中可能需要逐渐增加强度来保持相对稳定的体感强度。  
这有可能导致**在同一部位过长时间使用本产品后，真实刺激强度已经逐渐超过可承受的范围但是却没有被感觉到，**从而造成损伤。  
虽然本产品的最高输出严格低于安全标准的限制（r.m.s < 50ma，500Ω），但长时间使用仍然有可能造成损伤。因此，请在使用过程中**严格遵守连续使用时长的限制**。在同一部位连续使用**30分钟**后请休息一段时间，让感受灵敏度恢复到正常水平。
3. 连续不断的高频刺激会使使用部位快速适应，建议使用**频率不断变化且间歇休息**的波形，从而获得更好的使用体验。以每小段波形刺激时间1-10秒，休息1-10秒为宜。
