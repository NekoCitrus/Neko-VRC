# 波形库（实验功能）

## 使用

WinUI 3 主页点击“波形库”卡片（右侧箭头）。子页面标题为“基本设置 > 波形库”，直接点击“基本设置”返回，不再显示单独的返回键。左侧是未加入选择框的内置 / 导入波形，右侧为该设备 A/B 通道共用的可选列表。郊狼与负鼠两套列表完全隔离，不共享格式或排序。

- Ctrl / Shift 多选，可跨列表拖动，或使用中间箭头；右侧使用原生 ListView 拖动排序。
- 选择条目后，使用原始 25 ms 样本循环滚动最近 2 秒的联合示意图：郊狼柱高表示幅值，柱间距表示周期（越密频率越高），右端为当前时刻。预览区只显示波形名称、图形与播放 / 暂停图标，不显示坐标说明、数值、时长、图例或提示文字。逐样本积分频率，保持跨样本及循环边界的相位连续；最多约 201 柱，不预生成整个长波形的脉冲。切页停止动画，隐藏窗口暂停绘制。仅示意幅值与频率关系，并非真实电流形状，不发送设备指令、不跟随硬件输出。
- 官方周期字节先解压为 ms，再计算 Hz。官方“脉冲 / 压缩”幅值都是 100%，但疏密随频率变化；“树荫”是恒定 100% 幅值、10 Hz，不人为编造起伏。负鼠只有 25 ms 幅值柱，不把固定占位字节当作电刺激频率。
- 编辑立即保存；自定义波形切换沿用现有热更新，不断开 App / BLE，不清空当前深度。正在使用的波形不能移出，至少保留一项。
- 导入只加入波形库，不自动启用、不修改强度上限、不触发设备。
- ZIP 不解压到磁盘，仅读取其中 `.pulse`；文件夹递归读取。按设备和实际帧内容去重（文件名不同也会去重），同名不同内容会自动添加编号。格式错误单独跳过，提示导入 / 重复 / 无效数量，悬停提示可查看前八条失败原因。

文件保存在配置目录下 `waveforms/library.json`，包含自定义波形、设备类型和可选列表顺序。写入使用临时文件及原子替换；原 settings YAML 仅保存波形 ID，帧数据不随设置进入前端 IPC。迁移配置时先载入波形库，避免自定义选择被重置。备份时同时保存 YAML 和 `waveforms` 文件夹。

## 郊狼 .pulse

支持 `Dungeonlab+pulse:` 文本，包括旧导出中省略全局头部的版本。处理频率 / 时长索引、小节开关、四种频率变化、完整脉冲元循环、1/2/4 倍速及整个波形结束后的休息段。编辑器锚点只用于编辑，不单独改变输出幅值；最终生成 100 ms 帧（每帧四个 25 ms 样本）。不足一帧用零幅值补齐。

格式转换是独立实现，并非官方 App 播放引擎。资料中对部分版本的时长索引和渐变端点存在差异，因此不能保证与所有版本 App 的输出逐样本相同；2.0 主机继续沿用既有降采样近似，体感需实机验证。格式错误不猜测修正，不允许幅值超过 100 或非法频率。全局第三个值保留校验，不修改现有主机的平衡设置。

本地样例 `波形  数量150+.zip`：318 个 `.pulse`，转换后新增 232 个、重复 85 个、拒绝 1 个。被拒绝的“混乱涡旋”将小节开关写为 2～9，而非 0/1，未擅自解释这些数值。样例包未提交 / 分发。

## 帧交换格式与负鼠边界

可以使用 UTF-8 JSON 文本、扩展名 `.pulse`，例如负鼠：

```json
{"device_kind":"opossum","frames":["0A0A0A0A00643219"]}
```

郊狼使用 `"device_kind":"coyote"`，帧结构为四个压缩频率字节（10～240）和四个幅值字节（0～100）；负鼠必须固定前缀 `0A0A0A0A`，其后为四个振动幅值。文件名用作显示名。此格式是 Neko-VRC 的显式交换格式，不冒充官方负鼠 App 导出格式。负鼠自定义文件同样支持 ZIP / 文件夹导入与去重；不会把郊狼电刺激频率静默转换为振动。

边界：单文件 ≤1 MB、ZIP ≤32 MB、每次 ≤2000 个波形、每波形 ≤35000 帧；库总量 ≤500000 帧且清单 ≤16 MB。超限会拒绝保存该次导入，已有库保持不变。

## 实现依据

验证：266 项 Python 自动测试通过，包含四通道 PCS 与有效插座深度取最大值、关闭 / 过期和真实 UDP 路由；WinUI 构建无编译警告。界面回归包含根页 / 子页真实标题位置与基线（不容许一像素偏移）、原生箭头、PCS/LMS 与帮助图标的行列对齐、三种恒幅波形的联合图、100 Hz / 1 Hz 疏密与幅值、相位跨界 / 变化、SteamVR 保存失败后的开关回退、双主题、最小尺寸、预览暂停与切页清理、列表移动、导入去重、热应用和退出，并检查实际渲染截图。列表排序使用原生 ListView；未用实体鼠标验证所有拖动手势，未连接主机测试新波形的实际体感。官方负鼠 App 导出格式仍需提供样例后核对。

独立解析实现参考公开格式事实与官方协议，未复制第三方解析器代码：

- [.pulse 文件结构与滑块说明](https://github.com/MofuNadenade/DG-LAB-VRCOSC/blob/412158f8bddc59619a73cb9ce6b699dd63f26a7c/docs/protocol/%E9%83%8A%E7%8B%BCApp%E6%B3%A2%E5%BD%A2%E6%A0%BC%E5%BC%8F.md)
- [V3 时长映射对照](https://github.com/hyperzlib/DG-Lab-Coyote-Game-Hub/blob/adaf01193a7e1e0e865e0ec18371ade6edf49cc1/frontend/src/lib/dg-pulse-helper/DGLabPulseFileHelper.ts)
- [官方郊狼 3.0 协议](https://github.com/dungeonlab-open/dglab-bluetooth-protocol/tree/main/coyote/v3)
- [官方负鼠协议](https://github.com/dungeonlab-open/dglab-bluetooth-protocol/tree/main/opossum-vibrate-controller)
- [官方波形原始帧（含树荫、脉冲、压缩）](https://github.com/dungeonlab-open/dglab-kit/blob/e1707e6e7ef50bdbe8c8dafe19f34289e01dffc0/src/waveform/coyote.ts)
- [WinUI 原生面包屑模板](https://github.com/microsoft/microsoft-ui-xaml/blob/main/controls/dev/Breadcrumb/BreadcrumbBar.xaml) 与[官方主题资源](https://github.com/microsoft/microsoft-ui-xaml/blob/main/controls/dev/Breadcrumb/BreadcrumbBar_themeresources.xaml)：根页和子页共用模板、官方字号 / 字重 / 箭头资源，统一当前项与父项模板的高度和内边距，避免嵌套 Button 在分数 DPI 下逐层取整导致像素偏移。不自绘 `>`。
