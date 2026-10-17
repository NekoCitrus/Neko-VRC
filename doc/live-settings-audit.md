# 即时配置与数据链路检查

## 本轮变更

- WinUI 设置自动应用及持久化，连续编辑约每 120 ms 合并一次，非法输入保留旧配置并报错。主题单独保存；修改一般设置不清空设备输入。上限/波形修改保留深度；输入切换只失效相应来源。
- 电平 = 当前最大触发比例 × 对应主机的有效上限；同类多主机显示其中最大计算值。与采样波形幅值、主机反馈值是不同字段，暂停或未连接输出为零。
- 电平样式参考微软官方 [Slider 模板](https://github.com/microsoft/microsoft-ui-xaml/blob/main/controls/dev/CommonStyles/Slider_themeresources.xaml) 和 [MediaTransportControls 音量滑块](https://github.com/microsoft/microsoft-ui-xaml/blob/main/controls/dev/CommonStyles/MediaTransportControls_themeresources.xaml)：保留原生控件及交互，按参考截图在真实滑块移动范围内加入 6 px 加粗电平，共用原生强调色和视觉状态。并非取得或复制 Windows Shell 私有的音量面板源码。
- App 扫码按钮会先应用 App 路由；设备页仅切换自身类型，主页保留已连接的 BLE 设备。保持 controller ID、监听端口及既有 WebSocket 会话。
- 修复上限从 0 调高不恢复；空闲负鼠不因调上限启动。App 新连接只清理 App 路径旧触发，不干扰 BLE；已有 OSCQuery 链路失联 5 秒后清理旧模型输入。
- 每次启动优先独立写入程序同级 `log/`，不可写时回退配置目录下 `log/`；主程序条目标为 `neko-vrc`。显式测试配置目录保持隔离。强度按变更采样（最高 5 Hz），取消五秒无变化保活打印。调试页仅保留各通道计算值/真实回报值并自动跟随最新行；未回报不冒充零或发送目标。延迟是阶段耗时/ACK RTT，不代表硬件动作完成。
- 新增独立 5 秒 `OGB_ENABLED=true` 布尔 OSC 发送，通过 VRChat OSCQuery HOST_INFO 解析目标端口，不依赖 Chatbox/设备连接；服务停止后结束，不发送 false。不改变深度、触摸或摩擦支持范围。
- 连接栏展示各自的 App 请求确认/Ping RTT 或 BLE GATT 写入/入队耗时；ACK 样本超过 30 秒失效，无样本不显示假零值。数据随各主机快照进入计算强度日志，不推断 App 到主机的蓝牙延迟。

## OGB 对照

### 参数读取恢复

- UDP 参数仍即时处理，并保持对较旧 HTTP 快照的覆盖保护。250 ms 补读保留，避免静止参数过期；缓存成功端点，减少重复 mDNS 解析。
- 全量 JSON 截断/损坏时拆分读取 `/avatar/change` 与 `/avatar/parameters`，前后身份不一致则重试；隐藏身份不阻止有效参数读取。没有移植面捕输出逻辑。
- mDNS 不可用时，限频读取最新 VRChat 日志尾部的已声明端口，并验证 HOST_INFO 是 VRChat；不扫描任意端口。异常不会终止读取线程，五秒失联归零保护保持。

参考 [OGB GameDevice.ts（643aedd）](https://github.com/OscToys/OscGoesBrrr/blob/643aedd493ba21b0c0859ce6e57708fe408ee9c0/src/main/GameDevice.ts)。

- Self/Others 分别估长；Root/Tip 差值取最近 8 次样本中最接近的一对，至少 4 个常规样本；已插入时使用备用长度。
- 对齐 0.01、0.95、0.02、0.99 等边界与 `1 - (1-root)/length` 深度公式，优先新深度，无新深度才回退旧值。
- 本轮补齐旧 Socket `PenOthersClose` 门控；未知/过期的已声明门控不能重新激活旧深度。Plug 的 Self/Others 深度取最大值。
- 本软件将最终深度约束在 0～1，并增加输入时效、暂停与输出上限保护。仅对接 SPS 深度，不等同 OGB 全部 Touch/Frot/TPS 功能或其可选输出滤波。

## 验证范围

### 郊狼 3.0 独立蓝牙调查

- 发现旧 `broadcast_repeated_wave` 绕过路由，仅发给 App；改为经过 DeviceRouter，BLE 重复包按单帧 100 ms 发送。
- 蓝牙序列号一直为 0，不请求 B1；且先归零再订阅，无法捕获初始回报。现在先订阅，按需请求非零序列号确认；等待中继续发波形，超时可重试，归零优先。
- App V3/V4 曾使用乐观更新的目标值作为真实值，现在单独保存实际收到的回报。没有回报时显示未反馈。
- 固件模拟回归覆盖无 App 的插座 Root/Tip、插头、PCS、地图、旧 sendwave、初始软上限为零、反馈与暂停。没有发现 SPS 计算本身依赖 App 的条件；不能据此断言已经复现或彻底修复用户“计算值几乎一直为零”的实机现象，需要该用户的新文件日志核对输入比例、路由及连接错误。未擅自调整 BF 的体感平衡参数。

包含配置热更新、四通道隔离、蓝牙写入模拟、App V3/V4 配对与真实本地 Socket 报文、多槽位强度上限、地图事件、OSCQuery 读取及参数时效、深度边界和 GUI 绑定/导航/退出测试。未连接实体主机、手机 App 或 VRChat 实测，不能据此保证实际无线链路质量。
