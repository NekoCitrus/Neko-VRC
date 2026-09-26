# Neko-VRC
<img width="1102" height="750" alt="image" src="https://github.com/user-attachments/assets/636a17c1-cfc8-4a83-910e-6d861db08d8b" />
<img width="1102" height="750" alt="image" src="https://github.com/user-attachments/assets/1aa97c51-aa7f-43b4-a7c1-a790108d9daf" />
<img width="1102" height="750" alt="image" src="https://github.com/user-attachments/assets/c10c9228-88d1-4488-bee5-c0e53f47433b" />
<img width="1102" height="750" alt="image" src="https://github.com/user-attachments/assets/6396a63d-05ba-4a79-b396-1bafb5c2740b" />


[English version](README_en.md)

一个通过 VRChat OSC 读取 Avatar SPS/OGB 插入深度，并联动郊狼 DG-LAB 3.0 或负鼠振动控制器的小工具。

我们的 VRChat 群组： [ShockingVRC https://vrc.group/SHOCK.2911](https://vrc.group/SHOCK.2911)

> [!CAUTION]
> 您必须阅读并同意 [安全须知](doc/dglab/安全须知.md) ([Safety Precautions](doc/dglab/SafetyPrecautions.md) in English) 后才可以使用本工具！

## 使用方式

1. 前往 [本项目 Release](https://github.com/NekoCitrus/Neko-VRC/releases) 下载最新版本的 Neko-VRC
2. 运行 exe。程序会直接打开轻量桌面窗口并自动启动后台服务。
3. 在“郊狼 A/B”和“负鼠 A/B”中，分别为各自的 A/B 通道设置 Socket 或 Plug、部位、波形和强度上限。
4. 点击“保存并重启服务”。首次联网时如弹出 Windows 防火墙提示，请选择允许。
5. 启动最新版 DG-LAB 4 APP，在 APP 内用蓝牙连接郊狼或负鼠，再用 Socket 控制扫描程序二维码。
6. 如勾选“关闭主窗口后继续在系统托盘运行”，关闭窗口不会停止服务；可从托盘菜单重新打开或退出。

## 桌面窗口

- **基本设置**：编辑 Chatbox、后台运行与 SteamVR 跟随启动开关。设备由 DG-LAB 4 APP 通过 Socket V4 上报，程序通过 OSCQuery 供 VRChat 自动发现。
- **郊狼 A/B**：郊狼 A/B 各自设置 SPS 触发、部位、波形和强度上限；上限与 `intensityMax` 取较小值。
- **负鼠 A/B**：负鼠 A/B 也有完全独立的触发、部位、波形和强度上限。
- **运行调试**：显示设备连接、触发方式、选择范围、当前触发部位、深度与设备通道强度。
- **版权信息**：显示项目、代码来源、前端贡献者与开源许可。

程序当前接受一个 DG-LAB APP 连接，但会同时控制该 APP 上报的所有受支持郊狼与负鼠槽位。

### 跟随 SteamVR 启动

“基本设置”中的“跟随 SteamVR 启动”复选框通过 OpenVR 应用清单启用或关闭 SteamVR 自动启动。更改该选项时请先启动 SteamVR，再点击“保存并重启服务”。清单保存在 `%APPDATA%\ShockingVRChat\steamvr\`；程序启动时会重新校验并修复 exe 移动后的路径。

该功能不会使用 Windows 开机启动项，也不会自行启动或轮询 SteamVR。SteamVR 首次读取新清单时可能要求重启 SteamVR；窗口会显示相应提示，并在下一次启动时继续完成设置。

## 配置文件

配置固定保存在：

```text
%APPDATA%\ShockingVRChat\settings-v0.8.yaml
```

日志保存在同目录的 `neko-vrc.log`。为兼容旧版本，配置目录继续使用 `%APPDATA%\ShockingVRChat`。首次运行 v0.8 时会迁移 v0.7 及更旧配置；旧的共享 SPS 设置会复制为郊狼和负鼠各自的初始 A/B 设置。

## 设备控制方式

郊狼与负鼠都经同一条 DG-LAB 4 APP Socket V4 连接控制，但是 APP 将每台主机作为独立 `slotId` 上报。程序会同时保留所有 `COYOTE_020` / `COYOTE_030` 和 `OVC_1` 槽位，分别计算强度并按槽位发送任务。负鼠波形会自动转为 OVC 所用的固定前缀与四个振动强度字节。参见 [DG-LAB 官方 dglab-kit](https://github.com/dungeonlab-open/dglab-kit)。

一般设置，以及郊狼/负鼠各自的 A/B 触发、部位、波形和强度上限，建议直接在窗口修改。WebSocket 或 Web 服务端口等高级选项仍可在 YAML 中修改；手动修改 YAML 后请从托盘退出程序并重新打开。

## SPS 触发方式

每个 A/B 通道只能使用以下一种方式：

- **SPS Socket 被插入深度**：读取 `OGB/Orf/<部位>` 的 Root/Tip 数据，按 OGB 算法估算插入深度。
- **SPS Plug 插入深度**：读取 `OGB/Pen/<部位>` 的 `PenSelf/PenOthers` 深度。

每种方式都可选择一个指定部位，或选择“任何当前正在触发的”部位；后者会取所有当前触发部位中的最大深度。程序会主动发现 VRChat 的 OSCQuery 服务并查询 `/avatar`，从当前参数树读取 Avatar ID 与 SPS 部位；`/avatar/change` 仅用于要求立即刷新，查询失败时才作为兜底。对应 ID 的本地 OSC JSON 只用于补充友好名称。

两种方式最终都得到 `0～1` 的深度，并线性缩放各已连接主机的波形幅值。旧的自定义参数列表、distance 模式、shock 模式和 `trigger_range` 已移除。


## 配置文件参考

配置文件格式为 YAML，当前版本为 `v0.8`。郊狼和负鼠的 A/B 设置分别位于 `channels.dglab3.coyote` 和 `channels.dglab3.opossum`。

```yaml
version: v0.8
channels:
  version: v0.8
  dglab3:
    coyote:
      channel_a: {trigger_type: sps_socket, zone: '*', strength_limit: 100}
      channel_b: {trigger_type: sps_plug, zone: '*', strength_limit: 100}
    opossum:
      channel_a: {trigger_type: sps_plug, zone: '*', strength_limit: 100}
      channel_b: {trigger_type: sps_socket, zone: '*', strength_limit: 100}
settings:
  version: v0.8
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
- 程序会从 `/avatar/change` 对应的 OSC Avatar JSON 中提取部位列表；下拉框中的 `*` 表示“任何当前正在触发的部位”，不再接受手工 OSC 路径。
- 如果下拉列表为空，请先在 VRChat 中切换到目标 Avatar，并确认 VRChat 已生成对应的 OSC JSON。

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

涉及设备输出的 HTTP API 默认关闭。确实需要时才将 `control_enabled` 改为 `true`，并通过查询参数 `?token=...` 或请求头 `X-Control-Token` 提供令牌。请勿公开该令牌，也不建议将 Web 服务监听地址改成公网可访问地址。

## 开发与打包

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-build.txt
python -m unittest discover -v
pyinstaller --clean --noconfirm neko_vrc.spec
```

PyInstaller 生成无控制台的单文件 `dist\Neko-VRC.exe`，配置不会写在 exe 旁，而是固定写入兼容目录 `%APPDATA%\ShockingVRChat\`。

## FAQ

### 是否有逃生通道

- 郊狼可以按一下任意一侧肩键，此时 A/B 通道强度会被设置为 0。负鼠请使用 APP 停止输出或断开设备。
- 当程序检测到通道强度被用户主动设置为 0 后，将不再自动跟随强度上限。
- 还原需要手动在手机上点击 "+" 键，将通道强度 +1 ，即恢复自动跟随。

### 应该如何设置上限

- 建议通过郊狼 APP 内的被控设置进行调整，程序将跟随。
- 请在“郊狼 A/B”或“负鼠 A/B”页面分别调整对应设备和通道的强度上限；如需超过默认值 100，修改后保存并重启服务。
- 为保证强度自动跟随自动运行，请确认郊狼APP内 菜单-被控设置 中，两个通道的强度上限初始值（最小值）大于等于 1。

### 想用同一个 SPS 部位同时触发两个通道

- 在对应的“郊狼 A/B”或“负鼠 A/B”页面中，为 A、B 选择相同的触发方式和部位，然后保存并重启服务。

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

- 每台郊狼的通道强度取自身 `intensityMax` 与“郊狼 A/B”页面对应上限的较小值；负鼠使用“负鼠 A/B”页面中的独立上限。
- SPS 插入深度 `0～1` 线性缩放波形幅值；运行调试中会分别显示深度和设备通道强度。

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
