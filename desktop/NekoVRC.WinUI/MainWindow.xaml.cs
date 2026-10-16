using System;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using Microsoft.UI;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Media.Animation;
using Microsoft.UI.Xaml.Media.Imaging;
using Windows.Graphics;
using Windows.Graphics.Imaging;
using Windows.Storage;
using Windows.Storage.Streams;
using NekoVRC.WinUI.Views;
using BackendClient = NekoVRC.Desktop.BackendClient;
using Forms = System.Windows.Forms;

namespace NekoVRC.WinUI;

public sealed partial class MainWindow : Window
{
    private readonly BackendClient _client = new();
    private readonly Forms.NotifyIcon _tray = new();
    private readonly Microsoft.UI.Xaml.DispatcherTimer _timer = new() { Interval = TimeSpan.FromMilliseconds(600) };
    private bool _loaded, _polling, _exiting, _allowClose;
    private Task? _exitTask;
    private NavigationTransitionInfo? _lastPageTransition;
    private const int MinimumWidth = 880, MinimumHeight = 640;
    private const int DefaultWidth = 940, DefaultHeight = 648;
    private bool Preview => App.Arguments.FirstOrDefault() == "--preview-ui";
    public MainViewModel ViewModel { get; }

    public MainWindow()
    {
        InitializeComponent();
        ViewModel = new(_client); Root.DataContext = ViewModel;
        ViewModel.PropertyChanged += (_, args) => { if (args.PropertyName == nameof(MainViewModel.Theme)) ApplyTheme(); };
        ApplyTheme();
        ViewModel.QrReady += ShowQr;
        ViewModel.OperationError += message => { ErrorBar.Message = message; ErrorBar.IsOpen = message.Length > 0; };
        Title = "Neko-VRC";
        ExtendsContentIntoTitleBar = true;
        SetTitleBar(AppTitleBar);
        AppWindow.Resize(new SizeInt32(DefaultWidth, DefaultHeight));
        UpdateMinimumSize();
        string icon = Path.Combine(AppContext.BaseDirectory, "Assets", "neko-vrc.ico");
        if (File.Exists(icon)) AppWindow.SetIcon(icon);
        _tray.Icon = System.Drawing.Icon.ExtractAssociatedIcon(Environment.ProcessPath!) ?? System.Drawing.SystemIcons.Application;
        _tray.Text = "Neko-VRC";
        _tray.DoubleClick += (_, _) => DispatcherQueue.TryEnqueue(Restore);
        var menu = new Forms.ContextMenuStrip();
        menu.Items.Add("打开", null, (_, _) => DispatcherQueue.TryEnqueue(Restore));
        menu.Items.Add("暂停", null, (_, _) => DispatcherQueue.TryEnqueue(() => ViewModel.StopCommand.Execute(null)));
        menu.Items.Add("退出", null, (_, _) => DispatcherQueue.TryEnqueue(async () => await FinishExitAsync()));
        _tray.ContextMenuStrip = menu;
        AppWindow.Closing += (sender, e) => {
            if (_allowClose) return;
            e.Cancel = true;
            if (_exiting) return;
            if (ViewModel.Background && App.TestDirectory == null) { AppWindow.Hide(); _tray.Visible = true; }
            else _ = FinishExitAsync();
        };
        _timer.Tick += async (_, _) => {
            if (_polling || _exiting) return;
            _polling = true;
            try {
                if (Preview) await _client.CallAsync("snapshot");
                else await ViewModel.PollAsync();
            }
            catch (Exception exc) { _timer.Stop(); ReportFailure(exc); }
            finally { _polling = false; }
        };
    }

    private async void RootLoaded(object sender, RoutedEventArgs e)
    {
        if (_loaded) return;
        _loaded = true;
        Root.XamlRoot.Changed += (_, _) => UpdateMinimumSize();
        UpdateMinimumSize();
        double scale = Root.XamlRoot.RasterizationScale;
        AppWindow.Resize(new SizeInt32((int)Math.Ceiling(DefaultWidth * scale), (int)Math.Ceiling(DefaultHeight * scale)));
        try {
            if (App.TestDirectory != null) Directory.CreateDirectory(App.TestDirectory);
            await _client.OpenAsync(App.TestDirectory == null ? null : Path.Combine(App.TestDirectory, "config"));
            if (_exiting) return;
            await ViewModel.InitializeAsync();
            if (_exiting) return;
            Navigation.SelectedItem = Navigation.MenuItems[0];
            UpdateCaptionColors();
            if (App.TestDirectory != null) { await SmokeAsync(); return; }
            _timer.Start();
            await ViewModel.DeviceActionAsync("start");
        }
        catch (Exception exc) { ReportFailure(exc); }
    }

    public async void ReportFailure(Exception exc)
    {
        ErrorBar.Message = exc.Message; ErrorBar.IsOpen = true;
        if (App.TestDirectory != null) {
            File.WriteAllText(Path.Combine(App.TestDirectory, "error.txt"), exc + "\n" + _client.Diagnostics);
            await FinishExitAsync(1);
        }
    }

    private void ThemeChanged(FrameworkElement sender, object args) => UpdateCaptionColors();
    private void ApplyTheme() => Root.RequestedTheme = ViewModel.Theme switch { "light" => ElementTheme.Light, "dark" => ElementTheme.Dark, _ => ElementTheme.Default };
    private void UpdateCaptionColors()
    {
        if (AppWindow == null) return;
        var bar = AppWindow.TitleBar;
        bar.ButtonBackgroundColor = Colors.Transparent;
        bar.ButtonInactiveBackgroundColor = Colors.Transparent;
        bar.ButtonForegroundColor = Root.ActualTheme == ElementTheme.Dark ? Colors.White : Colors.Black;
    }
    private void PaneToggleRequested(TitleBar sender, object args) => Navigation.IsPaneOpen = !Navigation.IsPaneOpen;
    private void ContentFrameSizeChanged(object sender, SizeChangedEventArgs args)
    {
        // Keep the native page transition inside a stable viewport; cards cannot
        // slide over the fixed heading/actions when their page animates out.
        ContentFrame.Clip = new RectangleGeometry { Rect = new Windows.Foundation.Rect(0, 0, args.NewSize.Width, args.NewSize.Height) };
    }
    private void UpdateMinimumSize()
    {
        if (_exiting) return;
        if (AppWindow.Presenter is not OverlappedPresenter presenter) return;
        double scale = Root.XamlRoot?.RasterizationScale ?? 1;
        int width = (int)Math.Ceiling(MinimumWidth * scale);
        int height = (int)Math.Ceiling(MinimumHeight * scale);
        if (presenter.PreferredMinimumWidth != width) presenter.PreferredMinimumWidth = width;
        if (presenter.PreferredMinimumHeight != height) presenter.PreferredMinimumHeight = height;
    }
    private void NavigationChanged(NavigationView sender, NavigationViewSelectionChangedEventArgs e)
    {
        if (e.SelectedItem is not NavigationViewItem item || ViewModel == null) return;
        string tag = item.Tag?.ToString() ?? "general";
        // DevicePage captures its DeviceViewModel, so its connection card and
        // channels remain intact while the entire outgoing page animates away.
        ViewModel.SelectPage(tag);
        PageTitle.Text = item.Content?.ToString() ?? "";
        Type page = tag switch { "coyote" or "opossum" => typeof(DevicePage), "debug" => typeof(DebugPage), "about" => typeof(AboutPage), _ => typeof(GeneralPage) };
        _lastPageTransition = e.RecommendedNavigationTransitionInfo ?? new EntranceNavigationTransitionInfo();
        ContentFrame.Navigate(page, ViewModel, _lastPageTransition);
        ContentFrame.BackStack.Clear();
        ContentFrame.ForwardStack.Clear();
    }
    private void Restore() { if (_exiting) return; AppWindow.Show(); Activate(); _tray.Visible = false; }
    private Task FinishExitAsync(int code = 0) => _exitTask ??= ExitCoreAsync(code);
    private async Task ExitCoreAsync(int code)
    {
        _exiting = true; _timer.Stop();
        // Remove the window immediately, but retain the dispatcher/process until
        // output pause, zero commands, socket/BLE cleanup and backend exit finish.
        AppWindow.Hide(); _tray.Visible = false;
        try { _client.EmergencyStop(); await ViewModel.ThemeSaveTask; await _client.DisposeAsync(); }
        finally {
            _tray.Dispose(); Environment.ExitCode = code; _allowClose = true;
            Close(); Application.Current.Exit();
        }
    }
    private async void ShowQr(JsonArray matrix)
    {
        if (_exiting) return;
        try {
            double cell = 340.0 / matrix.Count;
            var canvas = new Canvas { Width = 340, Height = 340, Background = new SolidColorBrush(Colors.White) };
            for (int y = 0; y < matrix.Count; y++) for (int x = 0; x < matrix.Count; x++) {
                if (matrix[y]![x]!.GetValue<bool>()) {
                    var square = new Microsoft.UI.Xaml.Shapes.Rectangle { Width = cell, Height = cell, Fill = new SolidColorBrush(Colors.Black) };
                    Canvas.SetLeft(square, x * cell); Canvas.SetTop(square, y * cell); canvas.Children.Add(square);
                }
            }
            await new ContentDialog { Title = "App 扫码连接", Content = canvas, CloseButtonText = "关闭", XamlRoot = Root.XamlRoot }.ShowAsync();
        }
        catch (Exception exc) { ErrorBar.Message = exc.Message; ErrorBar.IsOpen = true; }
    }
    private static int FreePort(bool udp = false)
    {
        using var socket = new Socket(AddressFamily.InterNetwork, udp ? SocketType.Dgram : SocketType.Stream, udp ? ProtocolType.Udp : ProtocolType.Tcp);
        socket.Bind(new IPEndPoint(IPAddress.Loopback, 0)); return ((IPEndPoint)socket.LocalEndPoint!).Port;
    }
    // Integration harness: send the same message as the caption close button.
    // Window.Close() bypasses AppWindow.Closing and is only used after cleanup.
    [DllImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool PostMessageW(IntPtr window, uint message, IntPtr wParam, IntPtr lParam);
    private static System.Collections.Generic.IEnumerable<DependencyObject> Descendants(DependencyObject root)
    {
        for (int i = 0; i < VisualTreeHelper.GetChildrenCount(root); i++) {
            var child = VisualTreeHelper.GetChild(root, i); yield return child;
            foreach (var descendant in Descendants(child)) yield return descendant;
        }
    }
    private async Task CaptureAsync(string name)
    {
        var bitmap = new RenderTargetBitmap(); await bitmap.RenderAsync(Root);
        var buffer = await bitmap.GetPixelsAsync(); var bytes = new byte[buffer.Length];
        using (var reader = DataReader.FromBuffer(buffer)) reader.ReadBytes(bytes);
        var folder = await StorageFolder.GetFolderFromPathAsync(App.TestDirectory!);
        var file = await folder.CreateFileAsync(name + ".png", CreationCollisionOption.ReplaceExisting);
        using var stream = await file.OpenAsync(FileAccessMode.ReadWrite);
        var encoder = await BitmapEncoder.CreateAsync(BitmapEncoder.PngEncoderId, stream);
        encoder.SetPixelData(BitmapPixelFormat.Bgra8, BitmapAlphaMode.Premultiplied, (uint)bitmap.PixelWidth, (uint)bitmap.PixelHeight, 96, 96, bytes);
        await encoder.FlushAsync();
    }
    private async Task VerifyDeviceBindingsAsync(bool expand)
    {
        var device = ViewModel.CurrentDevice!;
        var boxes = Descendants(ContentFrame).OfType<ComboBox>().ToArray();
        var hostBox = boxes.Single(c => ReferenceEquals(c.ItemsSource, device.Devices));
        if (device.Devices.Count > 0 && (hostBox.SelectedItem as BluetoothDevice)?.Address != device.SelectedDevice?.Address) throw new Exception("Bluetooth host selection is not shown after navigation.");
        var mode = boxes.Single(c => ReferenceEquals(c.ItemsSource, device.ConnectionModes));
        if (mode.SelectedValue as string != device.Mode || mode.SelectedItem is not ConnectionMode choice || string.IsNullOrWhiteSpace(choice.Name)) throw new Exception("Connection mode selection/display lost.");
        string savedMode = device.Mode;
        mode.SelectedValue = "app";
        if (device.Mode != "app") throw new Exception("App mode selection failed.");
        mode.SelectedValue = "bluetooth";
        if (device.Mode != "bluetooth") throw new Exception("Bluetooth mode selection failed.");
        device.Mode = null!;
        if (device.Mode != "bluetooth") throw new Exception("Transient selection cleared the connection mode.");
        mode.SelectedValue = savedMode;
        foreach (var channel in device.Channels) {
            var waveform = boxes.Single(c => ReferenceEquals(c.ItemsSource, channel.WaveformChoices));
            if (waveform.SelectedValue as string != channel.Waveform || string.IsNullOrEmpty(channel.Waveform)) throw new Exception("Waveform selection lost.");
            if (waveform.ActualWidth > 131) throw new Exception("Waveform selector is not compact.");
            if (device.Kind == "coyote" && (channel.Waveforms[0] != "纯粹" || channel.Waveform != "纯粹")) throw new Exception("New Coyote profiles must default to the first Pure waveform.");
            var slider = Descendants(ContentFrame).OfType<Slider>().Single(s => ReferenceEquals(s.DataContext, channel));
            var input = Descendants(ContentFrame).OfType<TextBox>().Single(t => Microsoft.UI.Xaml.Automation.AutomationProperties.GetName(t) == "通道强度上限" && ReferenceEquals(t.DataContext, channel));
            string savedLimit = channel.Limit;
            if (slider.Minimum != 0 || slider.Maximum != 200 || slider.StepFrequency != 1 || slider.ActualWidth < 80) throw new Exception("Limit slider bounds or layout are wrong.");
            if (slider.TickPlacement != Microsoft.UI.Xaml.Controls.Primitives.TickPlacement.None || Descendants((Page)ContentFrame.Content).OfType<TextBlock>().Any(t => ReferenceEquals(t.DataContext, channel) && t.Text is "0" or "200")) throw new Exception("Slider ticks or endpoint labels were not removed.");
            foreach (int value in new[] { 0, 37, 200 }) {
                slider.Value = value;
                if (channel.Limit != value.ToString() || input.Text != value.ToString() || channel.Export().basic["strength_limit"]!.GetValue<int>() != value) throw new Exception("Slider did not update the limit input/export.");
            }
            input.Text = "83";
            if (slider.Value != 83) throw new Exception("Limit input did not update the slider.");
            input.Text = "";
            if (slider.Value != 83) throw new Exception("Empty input changed the last valid slider value.");
            try { channel.Export(); throw new Exception("Empty limit was accepted."); } catch (ArgumentException) { }
            input.Text = "201";
            try { channel.Export(); throw new Exception("Out-of-range limit was accepted."); } catch (ArgumentException) { }
            input.Text = savedLimit;
            var checks = Descendants(ContentFrame).OfType<CheckBox>().Where(c => ReferenceEquals(c.DataContext, channel)).ToArray();
            if (checks.Length != 5) throw new Exception("Channel trigger checkboxes missing.");
            var columns = Descendants((Page)ContentFrame.Content).OfType<Grid>().Single(g => g.Name == "TriggerColumns" && ReferenceEquals(g.DataContext, channel));
            var depths = columns.Children.OfType<Grid>().Single(g => g.Name == "DepthSelectors");
            var fallbacks = columns.Children.OfType<StackPanel>().Single(g => g.Name == "FallbackChoices");
            var extraGroup = columns.Children.OfType<StackPanel>().Single(g => g.Name == "ExtraChoice");
            var extra = extraGroup.Children.OfType<CheckBox>().Single();
            if (columns.ColumnDefinitions.Count != 3 || Grid.GetColumn(depths) != 0 || Grid.GetColumn(fallbacks) != 1 || Grid.GetColumn(extraGroup) != 2 || Descendants(fallbacks).OfType<CheckBox>().Count() != 2 || extra.Content?.ToString() != "参数 / 地图事件") throw new Exception("Trigger choices must occupy depth/fallback/map columns.");
            var help = Descendants(columns).OfType<Button>().ToArray();
            if (help.Length != 3 || !help.Select(b => ToolTipService.GetToolTip(b)?.ToString()).SequenceEqual(new[] { "当没有可计算深度的插座时使用PCS参数计算强度", "当没有可计算深度的插头时或者未处于吸附状态时使用LMS参数计算强度", "使用自定义参数和cheese郊狼地图事件触发" })) throw new Exception("Trigger help icons/tooltips are missing.");
            if (Descendants((Page)ContentFrame.Content).OfType<CommunityToolkit.WinUI.Controls.SettingsCard>().Any(c => ReferenceEquals(c.DataContext, channel) && c.Description != null)) throw new Exception("Channel status hint was not removed.");
            foreach (int row in new[] { 0, 1 }) {
                var depth = depths.Children.OfType<CheckBox>().Single(c => Grid.GetRow(c) == row);
                var zone = depths.Children.OfType<ComboBox>().Single(c => Grid.GetRow(c) == row);
                var depthOrigin = depth.TransformToVisual(columns).TransformPoint(default);
                var zoneOrigin = zone.TransformToVisual(columns).TransformPoint(default);
                if (zone.ActualWidth < 160 || zoneOrigin.X - depthOrigin.X - depth.ActualWidth > 7 || Math.Abs(zoneOrigin.Y + zone.ActualHeight / 2 - depthOrigin.Y - depth.ActualHeight / 2) > 1) throw new Exception("Depth checkbox/zone spacing or row alignment is incorrect.");
            }
            var fallbackOrigin = fallbacks.TransformToVisual(columns).TransformPoint(default);
            var extraOrigin = extraGroup.TransformToVisual(columns).TransformPoint(default);
            if (Math.Abs(fallbackOrigin.X - depths.ActualWidth - 20) > 1 || Math.Abs(extraOrigin.X - fallbackOrigin.X - fallbacks.ActualWidth - 20) > 1) throw new Exception("Trigger column gaps are inconsistent.");
            foreach (var check in checks) {
                bool original = check.IsChecked == true;
                check.IsChecked = !original;
                bool actual = check.Content?.ToString() switch {
                    "插座深度" => channel.SocketEnabled, "插头深度" => channel.PlugEnabled,
                    "PCS 回退" => channel.PcsEnabled, "LMS 回退" => channel.LmsEnabled,
                    _ => channel.ExtraEnabled,
                };
                if (actual == original) throw new Exception("Trigger checkbox TwoWay binding failed.");
                check.IsChecked = original;
            }
        }
        if (!expand) return;
        var expanders = Descendants(ContentFrame).OfType<Expander>().ToArray();
        if (expanders.Length != 2 || expanders.Any(e => e.IsExpanded)) throw new Exception("Custom parameter defaults are not collapsed.");
        if (expanders.Any(e => e.ActualHeight > 44)) throw new Exception("Custom parameter header is too tall.");
        foreach (var expander in expanders) expander.IsExpanded = true;
        await Task.Delay(750); Root.UpdateLayout();
        foreach (var channel in device.Channels) {
            var editor = Descendants(ContentFrame).OfType<TextBox>().Single(t => t.AcceptsReturn && ReferenceEquals(t.DataContext, channel));
            if (editor.FontFamily.Source != PageTitle.FontFamily.Source || editor.FontSize != 14) throw new Exception("Custom parameter font does not match standard controls.");
            string original = channel.ExtraPaths;
            editor.Text = "/avatar/parameters/Shock/TouchAreaA\n/avatar/parameters/Shock/wildcard/*";
            if (channel.Export().basic["extra_parameters"]!["paths"]!.AsArray().Count != 2) throw new Exception("Custom parameter TwoWay binding failed.");
            editor.Text = original;
            var limit = Descendants(ContentFrame).OfType<TextBox>().Single(t => Microsoft.UI.Xaml.Automation.AutomationProperties.GetName(t) == "通道强度上限" && ReferenceEquals(t.DataContext, channel));
            string originalLimit = channel.Limit;
            limit.Text = "75";
            if (channel.Export().basic["strength_limit"]!.GetValue<int>() != 75) throw new Exception("Limit TwoWay binding failed.");
            limit.Text = originalLimit;
            if (limit.ActualWidth > 81) throw new Exception("Toolkit minimum width overflows the limit column.");
        }
        await CaptureAsync("expanded-" + device.Kind);
        foreach (var expander in expanders) expander.IsExpanded = false;
        await Task.Delay(750);
    }
    private void VerifyHeaderAndPage(string tag)
    {
        bool isDevice = tag is "coyote" or "opossum";
        var page = (Page)ContentFrame.Content;
        var deviceViews = Descendants(page).OfType<DeviceView>().ToArray();
        if (deviceViews.Length != (isDevice ? 1 : 0)) throw new Exception("Wrong active connection view after navigation.");
        if (isDevice && (!ReferenceEquals(page.DataContext, ViewModel.CurrentDevice) || ViewModel.CurrentDevice?.Kind != tag)) throw new Exception("Device page is not pinned to the selected host.");
        if (isDevice) {
            var commands = Descendants(page).OfType<Button>().Where(b => b.Content is string).Select(b => b.Command).ToArray();
            if (commands.Length != 4 || !commands.SequenceEqual(new[] { ViewModel.CurrentDevice!.ScanCommand, ViewModel.CurrentDevice.ConnectCommand, ViewModel.CurrentDevice.DisconnectCommand, ViewModel.CurrentDevice.QrCommand })) throw new Exception("Connection actions are not in the device page.");
        }
        if (tag == "general" && Descendants(page).OfType<Button>().Count(b => Equals(b.Content, "App 扫码连接") && ReferenceEquals(b.Command, ViewModel.QrCommand)) != 1) throw new Exception("General QR action was not restored.");
        if (Descendants(HeaderActions).OfType<Button>().Count() != 3 || Descendants(HeaderActions).OfType<Button>().Any(b => Equals(b.Content, "退出"))) throw new Exception("Header must contain only Save/Pause/Resume.");
        if (_lastPageTransition == null || _lastPageTransition is SuppressNavigationTransitionInfo) throw new Exception("Page animation was disabled.");
        var actionsOrigin = GlobalActions.TransformToVisual(LayoutArea).TransformPoint(default);
        var statusOrigin = ServiceStatus.TransformToVisual(LayoutArea).TransformPoint(default);
        if (Math.Abs(actionsOrigin.X + GlobalActions.ActualWidth - LayoutArea.ActualWidth) > 1 || Math.Abs(actionsOrigin.Y + GlobalActions.ActualHeight / 2 - HeaderBar.ActualHeight / 2) > 1) throw new Exception("Action buttons are not fixed at the header's right edge.");
        if (Math.Abs(statusOrigin.X + ServiceStatus.ActualWidth + 12 - actionsOrigin.X) > 1 || Math.Abs(statusOrigin.Y + ServiceStatus.ActualHeight / 2 - actionsOrigin.Y - GlobalActions.ActualHeight / 2) > 1 || ServiceStatus.TextAlignment != TextAlignment.Right || ServiceStatus.ActualWidth < 80) throw new Exception($"Service/avatar status is not on the left of the same button row: {tag}, origin={statusOrigin}, size={ServiceStatus.ActualWidth}x{ServiceStatus.ActualHeight}, actions={actionsOrigin}, size={GlobalActions.ActualWidth}x{GlobalActions.ActualHeight}.");
        if (HeaderBar.ActualHeight > 37 || ContentFrame.Clip is not RectangleGeometry clip || Math.Abs(clip.Rect.Height - ContentFrame.ActualHeight) > 1) throw new Exception("Compact header or transition viewport is incorrect.");
        if (ContentFrame.BackStack.Count != 0 || ContentFrame.ForwardStack.Count != 0) throw new Exception("Menu navigation retained stale pages.");
    }
    private async Task SmokeAsync()
    {
        await _client.CallAsync("bluetooth_check");
        ViewModel.PrepareSmokePorts(FreePort(), FreePort(), FreePort(), FreePort(true));
        await ViewModel.SaveAsync(); await ViewModel.DeviceActionAsync("start"); await ViewModel.PollAsync();
        var qr = (await _client.CallAsync("qr"))["data"]!["matrix"]!.AsArray();
        if (qr.Count < 21 || qr.Any(row => row!.AsArray().Count != qr.Count)) throw new Exception("App QR matrix is invalid.");
        if (ViewModel.Devices.Count != 2 || ViewModel.Devices.Any(d => d.Channels.Count != 2)) throw new Exception("Four channels were not loaded.");
        if (ViewModel.Devices.Any(d => d.Mode != "bluetooth")) throw new Exception("New profiles must default to Bluetooth direct.");
        var savedAppDevice = new DeviceViewModel("coyote", ViewModel, new JsonObject { ["mode"] = "app", ["address"] = "", ["model"] = "coyote3" });
        if (savedAppDevice.Mode != "app") throw new Exception("Saved App mode was overwritten by the new default.");
        foreach (var device in ViewModel.Devices) foreach (var channel in device.Channels) {
            if (!channel.SocketEnabled || !channel.PlugEnabled || !channel.ExtraEnabled || channel.PcsEnabled != (device.Kind == "coyote") || channel.LmsEnabled != (device.Kind == "opossum")) throw new Exception("Channel defaults are wrong.");
        }
        if (!ExtendsContentIntoTitleBar || Navigation.GetType() != typeof(NavigationView)) throw new Exception("Stock WinUI navigation/title bar missing.");
        if (AppWindow.Presenter is not OverlappedPresenter presenter || presenter.PreferredMinimumWidth != (int)Math.Ceiling(MinimumWidth * Root.XamlRoot.RasterizationScale) || presenter.PreferredMinimumHeight != (int)Math.Ceiling(MinimumHeight * Root.XamlRoot.RasterizationScale)) throw new Exception("Native minimum window dimensions are not set.");
        if (PauseButton.Content?.ToString() != "暂停") throw new Exception("Pause button label is wrong.");
        if (ViewModel.StatusLine != ViewModel.Status + " · 等待读取模型 OSC 参数") throw new Exception("Waiting hint is not in the global status line.");
        var scanDevice = ViewModel.Devices[0]; string serviceStatus = ViewModel.Status;
        var scanResult = new TaskCompletionSource<JsonArray>(); var scanTask = scanDevice.ScanAsync(() => scanResult.Task);
        if (scanDevice.ScanStatus != "扫描蓝牙设备…" || ViewModel.Status != serviceStatus) throw new Exception("Scan status replaced service status.");
        scanResult.SetResult(new JsonArray(new JsonObject { ["device_kind"] = "coyote", ["address"] = "00:00:00:00:00:01", ["name"] = "测试郊狼", ["model"] = "coyote3" }));
        await scanTask;
        if (scanDevice.Devices.Count != 1 || ViewModel.Status != serviceStatus) throw new Exception("Scan results were not isolated.");
        await scanDevice.ScanAsync(() => Task.FromException<JsonArray>(new Exception("测试错误")));
        if (scanDevice.ScanStatus != "扫描失败：测试错误" || ViewModel.Status != serviceStatus) throw new Exception("Scan failure replaced service status.");
        var catalog = new JsonObject {
            ["avatar_name"] = "WinUI 3 测试 Avatar",
            ["sockets"] = new JsonArray(new JsonObject { ["zone_id"] = "Socket_One", ["label"] = "Socket 部位一" }, new JsonObject { ["zone_id"] = "Socket_Two", ["label"] = "Socket 部位二" }),
            ["plugs"] = new JsonArray(new JsonObject { ["zone_id"] = "Plug_One", ["label"] = "Plug 部位一" }),
        };
        var testState = new JsonObject { ["service"] = "running", ["avatar"] = catalog.DeepClone() };
        ViewModel.UpdateState(testState);
        if (ViewModel.StatusLine != ViewModel.Status + " · " + ViewModel.Avatar || ServiceStatus.Text != ViewModel.StatusLine) throw new Exception("Avatar status-line binding failed.");
        foreach (var (pcs, lms, expected) in new[] {
            (false, false, "无任何可用插座/插头参数"),
            (true, false, "推荐使用 PCS 回退"),
            (false, true, "推荐使用 LMS 回退"),
            (true, true, "推荐使用 PCS/LMS 回退"),
        }) {
            ViewModel.UpdateState(new JsonObject { ["service"] = "running", ["avatar"] = new JsonObject {
                ["avatar_name"] = "无深度测试 Avatar", ["sockets"] = new JsonArray(), ["plugs"] = new JsonArray(),
                ["parameter_availability"] = new JsonObject { ["known"] = true, ["socket"] = false, ["plug"] = false, ["pcs"] = pcs, ["lms"] = lms },
            } });
            if (ViewModel.ParameterHint != expected || ServiceStatus.Text != "服务运行中 · " + expected + " · 无深度测试 Avatar") throw new Exception("Global availability/fallback hint is wrong.");
        }
        ViewModel.UpdateState(testState);
        foreach (var channel in ViewModel.Devices.SelectMany(d => d.Channels)) {
            if (channel.SocketZones.Count != 3 || channel.PlugZones.Count != 2 || channel.SocketZones[2].Name != "Socket 部位二") throw new Exception("zone_id/label catalog not loaded.");
            channel.SocketZone = "Socket_Two"; channel.PlugZone = "Plug_One";
            channel.UpdateZones(null, null);
            if (channel.SocketZone != "Socket_Two" || !channel.SocketZones.Last().Name.EndsWith("（未检测到）")) throw new Exception("Missing selected zone lost.");
            channel.UpdateZones(catalog["sockets"]!.AsArray(), catalog["plugs"]!.AsArray());
        }
        var navigationItems = Navigation.MenuItems.OfType<NavigationViewItem>().Concat(Navigation.FooterMenuItems.OfType<NavigationViewItem>()).ToArray();
        if (navigationItems.Single(i => Equals(i.Tag, "coyote")).Content?.ToString() != "郊狼设置" || navigationItems.Single(i => Equals(i.Tag, "opossum")).Content?.ToString() != "负鼠设置") throw new Exception("Device navigation labels are wrong.");
        string[] channelStateBeforeNavigation = ViewModel.Devices.SelectMany(d => d.Channels).Select(c => { var data = c.Export(); return data.basic.ToJsonString() + data.runtime.ToJsonString(); }).ToArray();
        string[] connectionStateBeforeNavigation = ViewModel.Devices.Select(d => d.Mode + "|" + d.Address + "|" + d.SelectedDevice?.Address).ToArray();
        // During animations the outgoing page may still be visible, but its
        // connection card and channels must retain the SAME device together.
        for (int round = 0; round < 3; round++) foreach (string tag in new[] { "coyote", "general", "opossum", "debug", "coyote", "about", "opossum", "general" }) {
            var outgoing = ContentFrame.Content as DevicePage;
            var outgoingDevice = outgoing?.DataContext;
            Navigation.SelectedItem = navigationItems.Single(i => Equals(i.Tag, tag));
            Root.UpdateLayout();
            VerifyHeaderAndPage(tag);
            if (outgoing != null && !ReferenceEquals(outgoing.DataContext, outgoingDevice)) throw new Exception("Outgoing device binding changed during its animation.");
        }
        string[] channelStateAfterNavigation = ViewModel.Devices.SelectMany(d => d.Channels).Select(c => { var data = c.Export(); return data.basic.ToJsonString() + data.runtime.ToJsonString(); }).ToArray();
        if (!channelStateBeforeNavigation.SequenceEqual(channelStateAfterNavigation) || !connectionStateBeforeNavigation.SequenceEqual(ViewModel.Devices.Select(d => d.Mode + "|" + d.Address + "|" + d.SelectedDevice?.Address))) {
            File.WriteAllText(Path.Combine(App.TestDirectory!, "navigation-state.json"), System.Text.Json.JsonSerializer.Serialize(new { channelStateBeforeNavigation, channelStateAfterNavigation, connectionStateBeforeNavigation, connectionStateAfterNavigation = ViewModel.Devices.Select(d => d.Mode + "|" + d.Address + "|" + d.SelectedDevice?.Address).ToArray() }, new System.Text.Json.JsonSerializerOptions { WriteIndented = true }));
            throw new Exception("Navigation reset unsaved channel or connection settings.");
        }
        foreach (var theme in new[] { ElementTheme.Light, ElementTheme.Dark }) {
            ViewModel.Theme = theme == ElementTheme.Light ? "light" : "dark";
            // RenderTargetBitmap does not capture the DWM Mica surface.
            Root.Background = new SolidColorBrush(theme == ElementTheme.Dark ? Windows.UI.Color.FromArgb(255, 32, 32, 32) : Windows.UI.Color.FromArgb(255, 243, 243, 243));
            foreach (var size in new[] { new SizeInt32(DefaultWidth, DefaultHeight), new SizeInt32(1120, 920), new SizeInt32(MinimumWidth, MinimumHeight) }) {
                string suffix = size.Width == 940 ? "" : size.Width == 1120 ? "-large" : "-minimum";
                double scale = Root.XamlRoot.RasterizationScale;
                AppWindow.Resize(new SizeInt32((int)Math.Ceiling(size.Width * scale), (int)Math.Ceiling(size.Height * scale)));
                foreach (var item in navigationItems) {
                    Navigation.SelectedItem = item; await Task.Delay(750); Root.UpdateLayout();
                    if (ContentFrame.Content is not Page page || !ReferenceEquals(page.DataContext, item.Tag?.ToString() is "coyote" or "opossum" ? (object?)ViewModel.CurrentDevice : ViewModel)) throw new Exception("Navigation did not load the bound page.");
                    if (Navigation.Header != null || PageTitle.Text != item.Content?.ToString() || Math.Abs(PageTitle.TransformToVisual(Root).TransformPoint(default).X - ContentFrame.TransformToVisual(Root).TransformPoint(default).X) > 1) throw new Exception("Page title is not aligned with page content.");
                    VerifyHeaderAndPage(item.Tag!.ToString()!);
                    if (item.Tag?.ToString() == "general") {
                        var themeBox = Descendants((Page)ContentFrame.Content).OfType<ComboBox>().Single(c => Microsoft.UI.Xaml.Automation.AutomationProperties.GetName(c) == "外观模式");
                        string savedTheme = ViewModel.Theme;
                        foreach (string selectedTheme in new[] { "light", "dark", "system" }) {
                            themeBox.SelectedValue = selectedTheme;
                            if (ViewModel.Theme != selectedTheme || Root.RequestedTheme != (selectedTheme == "light" ? ElementTheme.Light : selectedTheme == "dark" ? ElementTheme.Dark : ElementTheme.Default)) throw new Exception("Theme selector did not immediately update the root theme.");
                        }
                        ViewModel.Theme = null!;
                        if (ViewModel.Theme != "system") throw new Exception("Transient binding cleared the theme choice.");
                        ViewModel.Theme = savedTheme;
                        await ViewModel.ThemeSaveTask;
                        var themeState = (await _client.CallAsync("init"))["data"]!;
                        if (themeState["settings"]!["general"]!["theme"]!.GetValue<string>() != savedTheme) throw new Exception("Theme was not automatically persisted without Save.");
                        Root.UpdateLayout();
                    }
                    string avatarBeforeLayoutCheck = ViewModel.Avatar;
                    ViewModel.Avatar = string.Concat(Enumerable.Repeat("很长的 Avatar 名称 Long Avatar Name · ", 12));
                    Root.UpdateLayout();
                    VerifyHeaderAndPage(item.Tag!.ToString()!);
                    if (!Equals(ToolTipService.GetToolTip(ServiceStatus), ViewModel.StatusLine)) throw new Exception("Full status/avatar tooltip is missing.");
                    ViewModel.Avatar = avatarBeforeLayoutCheck;
                    Root.UpdateLayout();
                    if (size.Width == DefaultWidth && item.Tag?.ToString() is "coyote" or "opossum") {
                        var cards = Descendants((Page)ContentFrame.Content).OfType<CommunityToolkit.WinUI.Controls.SettingsCard>().ToArray();
                        var lastCard = cards.Last();
                        double cardBottom = lastCard.TransformToVisual(Root).TransformPoint(default).Y + lastCard.ActualHeight;
                        double aboutBottom = AboutNavigationItem.TransformToVisual(Root).TransformPoint(default).Y + AboutNavigationItem.ActualHeight;
                        File.AppendAllText(Path.Combine(App.TestDirectory!, "layout.txt"), $"{theme} {item.Tag}: card bottom={cardBottom}, about bottom={aboutBottom}\n");
                        if (Math.Abs(cardBottom - aboutBottom) > 4) throw new Exception($"Default footer/card bottoms are not aligned: {cardBottom} vs {aboutBottom}.");
                        if (Descendants((Page)ContentFrame.Content).OfType<ScrollViewer>().Any(s => Equals(s.Tag, "PageScroll") && s.ScrollableHeight > 1)) throw new Exception("Default collapsed device settings should fit without scrolling.");
                    }
                    if (Descendants(ContentFrame).OfType<ScrollViewer>().Any(s => Equals(s.Tag, "PageScroll") && s.ComputedHorizontalScrollBarVisibility == Visibility.Visible)) throw new Exception("Unexpected page horizontal scrollbar.");
                    if (Descendants(ContentFrame).OfType<ScrollViewer>().Any(s => Equals(s.Tag, "PageScroll") && s.ActualHeight > ContentFrame.ActualHeight + 1)) throw new Exception("Page scrolling is not constrained to the content frame.");
                    if (item.Tag?.ToString() is "coyote" or "opossum") {
                        var boxes = Descendants(ContentFrame).OfType<ComboBox>().Where(c => Microsoft.UI.Xaml.Automation.AutomationProperties.GetName(c) is "插座部位" or "插头部位").ToArray();
                        if (boxes.Length != 4 || boxes.Any(c => c.SelectedValue as string != (ReferenceEquals(c.ItemsSource, ((ChannelViewModel)c.DataContext).SocketZones) ? "Socket_Two" : "Plug_One"))) throw new Exception("Socket/Plug ComboBox selection binding failed.");
                        await VerifyDeviceBindingsAsync(theme == ElementTheme.Light && size.Width == 940);
                    }
                    if (item.Tag?.ToString() == "debug") {
                        var log = Descendants(ContentFrame).OfType<TextBox>().Single(t => Microsoft.UI.Xaml.Automation.AutomationProperties.GetName(t) == "运行日志");
                        if (log.FontFamily.Source != PageTitle.FontFamily.Source || log.FontSize != 14) throw new Exception("Debug log does not use the standard control font.");
                        if (Descendants((Page)ContentFrame.Content).OfType<CommunityToolkit.WinUI.Controls.SettingsCard>().Any() || log.ActualHeight < ContentFrame.ActualHeight - 9 || log.ActualWidth < ContentFrame.ActualWidth - 1 || !log.IsReadOnly) throw new Exception("Debug page must contain only the full-height read-only log.");
                        ViewModel.Logs = "日志显示测试\n第二条打印信息";
                        if (log.Text.Replace("\r\n", "\n").Replace('\r', '\n') != ViewModel.Logs) throw new Exception("Log binding is not updated: " + System.Text.Json.JsonSerializer.Serialize(log.Text));
                    }
                    await CaptureAsync($"{theme.ToString().ToLowerInvariant()}-{item.Tag}{suffix}");
                }
            }
        }
        ViewModel.Theme = "system";
        Root.Background = null;
        var changedChannel = ViewModel.Devices[0].Channels[0]; changedChannel.SocketEnabled = false;
        ViewModel.Theme = "dark";
        await ViewModel.SaveAsync();
        var persisted = (await _client.CallAsync("init"))["data"]!;
        if (persisted["settings"]!["general"]!["theme"]!.GetValue<string>() != "dark") throw new Exception("Theme setting was not persisted.");
        if (persisted["basic"]!["dglab3"]!["coyote"]!["channel_a"]!["socket_enabled"]!.GetValue<bool>()) throw new Exception("Hot save did not persist.");
        ViewModel.StopCommand.Execute(null); await Task.Delay(300); await ViewModel.PollAsync();
        if (!ViewModel.Status.Contains("已暂停")) throw new Exception("Independent pause event failed.");
        await ViewModel.DeviceActionAsync("resume");
        string result = "PASS: global waiting/availability/fallback hints after service status, no repeated channel descriptions; three accessible help icons/tooltips per channel; three aligned trigger columns and tick-free 0-200 sliders; immediate theme autosave; log-only debug; default 940x648 footer/card alignment without scrolling; native transitions and 24 rapid switches preserve settings; five pages in both themes at three sizes; QR, hot save, pause/resume and immediate-hide shutdown.\n";
        if (Preview) {
            changedChannel.SocketEnabled = true; ViewModel.Background = false;
            ViewModel.UpdateState(testState); Navigation.SelectedItem = navigationItems[0];
            ViewModel.Status = "WinUI 3 预览 · 隔离配置，未连接设备";
            File.WriteAllText(Path.Combine(App.TestDirectory!, "result.txt"), "PASS: isolated WinUI preview and page/binding checks.\n");
            AppWindow.Resize(new SizeInt32((int)Math.Ceiling(DefaultWidth * Root.XamlRoot.RasterizationScale), (int)Math.Ceiling(DefaultHeight * Root.XamlRoot.RasterizationScale))); _timer.Start(); return;
        }
        var closeTimer = System.Diagnostics.Stopwatch.StartNew();
        Task shutdown;
        if (App.Arguments[0] == "--smoke-test-close") {
            if (!PostMessageW(WinRT.Interop.WindowNative.GetWindowHandle(this), 0x0010, IntPtr.Zero, IntPtr.Zero)) throw new Exception("Could not send caption close message.");
            await Task.Delay(100);
            shutdown = _exitTask ?? throw new Exception("Window close did not start graceful shutdown.");
        }
        else shutdown = FinishExitAsync();
        if (AppWindow.IsVisible || closeTimer.ElapsedMilliseconds > 250) throw new Exception("Window did not hide immediately before backend cleanup.");
        if (!ReferenceEquals(shutdown, FinishExitAsync())) throw new Exception("Duplicate exit does not share cleanup task.");
        File.WriteAllText(Path.Combine(App.TestDirectory!, "result.txt"), result + $"Window hidden within {closeTimer.ElapsedMilliseconds} ms.\n");
        await shutdown;
    }
}
