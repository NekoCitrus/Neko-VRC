using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Documents;
using System.Windows.Navigation;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Shapes;
using System.Windows.Threading;
using Forms = System.Windows.Forms;

namespace NekoVRC.Desktop;

public partial class MainWindow : Window
{
    private readonly BackendClient _client = new();
    private readonly MainViewModel _vm;
    private readonly DispatcherTimer _timer = new() { Interval = TimeSpan.FromMilliseconds(600) };
    private readonly Forms.NotifyIcon _tray = new();
    private bool _exiting, _polling;
    private readonly string? _smokeDir;
    private readonly bool _previewOnly;
    public MainWindow(string[] args)
    {
        InitializeComponent();
        _previewOnly = args.Length == 2 && args[0] == "--preview-ui";
        _smokeDir = args.Length == 2 && (args[0] == "--smoke-test" || _previewOnly) ? System.IO.Path.GetFullPath(args[1]) : null;
        _vm = new(_client); DataContext = _vm; _vm.QrReady += ShowQr;
        _tray.Icon = System.Drawing.Icon.ExtractAssociatedIcon(Environment.ProcessPath!) ?? System.Drawing.SystemIcons.Application;
        _tray.Text = "Neko-VRC";
        _tray.DoubleClick += (_, _) => Dispatcher.Invoke(Restore);
        var menu = new Forms.ContextMenuStrip();
        menu.Items.Add("打开", null, (_, _) => Dispatcher.Invoke(Restore));
        menu.Items.Add("急停", null, (_, _) => Dispatcher.Invoke(() => _vm.StopCommand.Execute(null)));
        menu.Items.Add("退出", null, (_, _) => Dispatcher.Invoke(() => BeginExit()));
        _tray.ContextMenuStrip = menu;
        Closing += OnClosing;
        Loaded += async (_, _) => {
            try {
                if (_smokeDir != null) Directory.CreateDirectory(_smokeDir);
                await _client.OpenAsync(_smokeDir == null ? null : System.IO.Path.Combine(_smokeDir, "config"));
                await _vm.InitializeAsync();
                if (_smokeDir != null) { await SmokeAsync(); return; }
                _timer.Start();
                await _vm.DeviceActionAsync("start");
            }
            catch (Exception exc) {
                _vm.Status = exc.Message;
                if (_smokeDir != null) {
                    File.WriteAllText(System.IO.Path.Combine(_smokeDir, "error.txt"), exc.ToString() + "\n" + _client.Diagnostics);
                    await FinishExitAsync(1);
                }
            }
        };
        _timer.Tick += async (_, _) => {
            if (_polling) return;
            _polling = true;
            try {
                // Keep the isolated preview backend alive without replacing
                // its sample catalog with a running game's avatar snapshots.
                if (_previewOnly) await _client.CallAsync("snapshot");
                else await _vm.PollAsync();
            }
            catch (Exception exc) { _timer.Stop(); _vm.Status = exc.Message; }
            finally { _polling = false; }
        };
    }

    private void Restore() { Show(); WindowState = WindowState.Normal; Activate(); _tray.Visible = false; }
    private void OnClosing(object? sender, CancelEventArgs e)
    {
        if (_exiting) return;
        e.Cancel = true;
        if (_vm.Background && _smokeDir == null) { Hide(); _tray.Visible = true; }
        else BeginExit();
    }
    private async void BeginExit() { if (!_exiting) await FinishExitAsync(0); }
    private async Task FinishExitAsync(int code)
    {
        _exiting = true; _timer.Stop();
        await _client.DisposeAsync();
        _tray.Dispose(); Close(); Application.Current.Shutdown(code);
    }
    private void ExitApp(object sender, RoutedEventArgs e) => BeginExit();
    private void OpenCredit(object sender, RequestNavigateEventArgs e)
    {
        if (e.Uri.Scheme != Uri.UriSchemeHttps) return;
        try { Process.Start(new ProcessStartInfo(e.Uri.AbsoluteUri) { UseShellExecute = true }); }
        catch (Exception exc) { _vm.Status = "无法打开链接：" + exc.Message; }
        e.Handled = true;
    }

    private void ShowQr(JsonArray matrix)
    {
        int size = matrix.Count;
        var drawing = new DrawingGroup();
        using (DrawingContext context = drawing.Open()) {
            context.DrawRectangle(Brushes.White, null, new Rect(0, 0, size, size));
            for (int y = 0; y < size; y++) for (int x = 0; x < size; x++)
                if (matrix[y]![x]!.GetValue<bool>()) context.DrawRectangle(Brushes.Black, null, new Rect(x, y, 1, 1));
        }
        var image = new Image { Source = new DrawingImage(drawing), Width = 340, Height = 340, Margin = new Thickness(20) };
        RenderOptions.SetBitmapScalingMode(image, BitmapScalingMode.NearestNeighbor);
        new Window { Title = "App 扫码连接", Content = image, SizeToContent = SizeToContent.WidthAndHeight, Owner = this, WindowStartupLocation = WindowStartupLocation.CenterOwner }.Show();
    }

    private static int FreePort(bool udp = false)
    {
        using var socket = new Socket(AddressFamily.InterNetwork, udp ? SocketType.Dgram : SocketType.Stream, udp ? ProtocolType.Udp : ProtocolType.Tcp);
        socket.Bind(new IPEndPoint(IPAddress.Loopback, 0)); return ((IPEndPoint)socket.LocalEndPoint!).Port;
    }

    // Check allocated bounds, not just screenshots: a hidden scrollbar must not hide controls.
    private static void ValidateLayout(FrameworkElement parent)
    {
        for (int i = 0; i < VisualTreeHelper.GetChildrenCount(parent); i++) {
            if (VisualTreeHelper.GetChild(parent, i) is not FrameworkElement child || !child.IsVisible) continue;
            Rect bounds = child.TransformToAncestor(parent).TransformBounds(new Rect(child.RenderSize));
            if (bounds.Left < -1 || bounds.Top < -1 || bounds.Right > parent.ActualWidth + 1 || bounds.Bottom > parent.ActualHeight + 1)
                throw new Exception($"Layout overflow: {child.GetType().Name} {bounds} in {parent.GetType().Name} {parent.RenderSize}");
            // Editors and dropdowns may scroll internally; the page itself must not.
            if (child is TextBox || child is ComboBox || child is Button || child is CheckBox || child is TextBlock || child is Expander) continue;
            if (child is ScrollViewer scroll) {
                if (!Equals(scroll.Tag, "OptionalChannelScroll")) throw new Exception("Unexpected page scrollbar.");
                if (scroll.ComputedVerticalScrollBarVisibility == Visibility.Visible &&
                    !VisualDescendants(scroll).OfType<Expander>().Any(e => e.IsExpanded)) throw new Exception("Collapsed channel settings should fit without a scrollbar.");
                continue;
            }
            ValidateLayout(child);
        }
    }

    private static IEnumerable<DependencyObject> VisualDescendants(DependencyObject parent)
    {
        for (int i = 0; i < VisualTreeHelper.GetChildrenCount(parent); i++) {
            var child = VisualTreeHelper.GetChild(parent, i);
            yield return child;
            foreach (var descendant in VisualDescendants(child)) yield return descendant;
        }
    }

    private async Task ValidateZoneDropdownsAsync()
    {
        var boxes = VisualDescendants(PageArea).OfType<ComboBox>().Where(c => c.ItemsSource is IEnumerable<Zone>).ToArray();
        if (boxes.Length != 4) throw new Exception("Expected Socket/Plug dropdowns for both channels.");
        foreach (var box in boxes) {
            var channel = (ChannelViewModel)box.DataContext;
            bool socket = ReferenceEquals(box.ItemsSource, channel.SocketZones);
            string expected = socket ? channel.SocketZone : channel.PlugZone;
            if (box.SelectedValue as string != expected || box.SelectedItem is not Zone selected ||
                selected.Name != (socket ? "Socket 部位二" : "Plug 部位一")) throw new Exception("Selected Socket/Plug label was not displayed.");
            box.IsDropDownOpen = true;
            await Dispatcher.InvokeAsync(() => UpdateLayout(), DispatcherPriority.Render);
            await Task.Delay(100);
            foreach (Zone zone in box.Items) {
                var item = box.ItemContainerGenerator.ContainerFromItem(zone) as ComboBoxItem;
                if (item == null || !VisualDescendants(item).OfType<TextBlock>().Any(t => t.Text == zone.Name)) throw new Exception("Dropdown zone label is missing: " + zone.Name);
            }
            if (channel.Name.StartsWith("A") && box.Template.FindName("PART_Popup", box) is System.Windows.Controls.Primitives.Popup popup && popup.Child is FrameworkElement content) {
                var bitmap = new RenderTargetBitmap((int)Math.Ceiling(content.ActualWidth), (int)Math.Ceiling(content.ActualHeight), 96, 96, PixelFormats.Pbgra32);
                bitmap.Render(content);
                var encoder = new PngBitmapEncoder(); encoder.Frames.Add(BitmapFrame.Create(bitmap));
                using var file = File.Create(System.IO.Path.Combine(_smokeDir!, socket ? "socket-dropdown.png" : "plug-dropdown.png")); encoder.Save(file);
            }
            box.IsDropDownOpen = false;
        }
    }

    private async Task SmokeAsync()
    {
        await _client.CallAsync("bluetooth_check");
        _vm.PrepareSmokePorts(FreePort(), FreePort(), FreePort(), FreePort(true));
        await _vm.SaveAsync(); await _vm.DeviceActionAsync("start"); await _vm.PollAsync();
        if (_vm.Devices.Count != 2 || _vm.Devices.Any(d => d.Channels.Count != 2)) throw new Exception("通道数量错误。");
        if (VisualDescendants(this).OfType<Button>().Any(button => Equals(button.Content, "启动服务"))) throw new Exception("Removed start-service button is still visible.");
        // Simulate scan results without scanning/connecting physical hardware.
        var scanDevice = _vm.Devices[0];
        string serviceStatus = _vm.Status;
        var pendingScan = new TaskCompletionSource<JsonArray>();
        var scanTask = scanDevice.ScanAsync(() => pendingScan.Task);
        if (scanDevice.ScanStatus != "扫描蓝牙设备…" || _vm.Status != serviceStatus) throw new Exception("Scan progress replaced service status.");
        await _vm.PollAsync();
        if (scanDevice.ScanStatus != "扫描蓝牙设备…") throw new Exception("Polling erased scan progress.");
        pendingScan.SetResult(new JsonArray(
            new JsonObject { ["device_kind"] = "coyote", ["address"] = "00:00:00:00:00:01", ["name"] = "测试郊狼", ["model"] = "coyote3" },
            new JsonObject { ["device_kind"] = "opossum", ["address"] = "00:00:00:00:00:02", ["name"] = "测试负鼠", ["model"] = "opossum" }));
        await scanTask;
        if (scanDevice.Devices.Count != 1 || scanDevice.ScanStatus != "扫描完成，找到 1 台设备" || _vm.Status != serviceStatus || _vm.Devices[1].ScanStatus != "") throw new Exception("Scan result was not isolated per device.");
        await scanDevice.ScanAsync(() => Task.FromResult(new JsonArray()));
        if (!scanDevice.ScanStatus.StartsWith("未找到设备") || _vm.Status != serviceStatus) throw new Exception("Empty scan changed service status.");
        await scanDevice.ScanAsync(() => Task.FromException<JsonArray>(new Exception("测试错误")));
        if (scanDevice.ScanStatus != "扫描失败：测试错误" || _vm.Status != serviceStatus) throw new Exception("Scan failure changed service status.");
        await scanDevice.ScanAsync(() => Task.FromResult(new JsonArray()));
        foreach (var device in _vm.Devices) foreach (var channel in device.Channels) {
            if (!channel.SocketEnabled || !channel.PlugEnabled || !channel.ExtraEnabled ||
                channel.PcsEnabled || channel.LmsEnabled) throw new Exception("默认开关错误。");
        }
        if (TryFindResource("DefaultComboBoxStyle") is not System.Windows.Style || TryFindResource("CardBackgroundFillColorDefaultBrush") is not Brush) throw new Exception("Fluent theme resources were not loaded.");
        if (WindowStyle != WindowStyle.SingleBorderWindow || ResizeMode != ResizeMode.CanResize || AllowsTransparency) throw new Exception("Native window frame was not preserved.");
        if (_vm.Credits.Count != 11 || _vm.Credits.Any(c => !Uri.TryCreate(c.Url, UriKind.Absolute, out var uri) || uri.Scheme != "https")) throw new Exception("Invalid copyright links.");
        // Synthetic catalog hints only: never inject trigger values or connect
        // hardware. Verify the backend-state binding and representative widths.
        string[] hints = { "无 Socket/Plug 深度；可启用 PCS 回退", "无 Socket/Plug 深度；可启用 LMS 回退", "无可用的 Socket、Plug、PCS、LMS 参数", "" };
        var channels = new JsonObject();
        int hintIndex = 0;
        foreach (var device in _vm.Devices) {
            var deviceChannels = new JsonObject();
            foreach (var channel in device.Channels)
                deviceChannels[channel.Name[..1]] = new JsonObject { ["parameter_hint"] = hints[hintIndex++] };
            channels[device.Kind] = deviceChannels;
        }
        var catalog = new JsonObject {
            ["avatar_name"] = "下拉框测试 Avatar",
            ["sockets"] = new JsonArray(
                new JsonObject { ["zone_id"] = "Socket_One", ["label"] = "Socket 部位一" },
                new JsonObject { ["zone_id"] = "Socket_Two", ["label"] = "Socket 部位二" }),
            ["plugs"] = new JsonArray(new JsonObject { ["zone_id"] = "Plug_One", ["label"] = "Plug 部位一" }),
        };
        _vm.UpdateState(new JsonObject { ["service"] = "running", ["avatar"] = catalog, ["runtime"] = new JsonObject { ["device_channels"] = channels } });
        if (!_vm.Devices.SelectMany(d => d.Channels).Select(c => c.ParameterHint).SequenceEqual(hints)) throw new Exception("Parameter hints were not bound to the correct channels.");
        foreach (var channel in _vm.Devices.SelectMany(d => d.Channels)) {
            if (channel.SocketZones.Count != 3 || channel.PlugZones.Count != 2 ||
                channel.SocketZones[2].Name != "Socket 部位二" || channel.PlugZones[1].Name != "Plug 部位一") throw new Exception("Backend zone_id/label catalog was not displayed.");
            channel.SocketZone = "Socket_Two"; channel.PlugZone = "Plug_One";
            channel.UpdateZones(null, null);
            if (channel.SocketZone != "Socket_Two" || channel.PlugZone != "Plug_One" ||
                !channel.SocketZones.Last().Name.EndsWith("（未检测到）")) throw new Exception("Missing configured zones were lost.");
            channel.UpdateZones(catalog["sockets"]!.AsArray(), catalog["plugs"]!.AsArray());
            var originalSocket = channel.SocketZones[2];
            channel.UpdateZones(catalog["sockets"]!.AsArray(), catalog["plugs"]!.AsArray());
            if (!ReferenceEquals(originalSocket, channel.SocketZones[2])) throw new Exception("Unchanged zone catalog was unnecessarily rebuilt.");
            channel.SocketZone = null!; channel.PlugZone = null!; // WPF's transient deselection during collection changes.
            var exported = channel.Export().basic;
            if (exported["socket_zone"]!.GetValue<string>() != "Socket_Two" ||
                exported["plug_zone"]!.GetValue<string>() != "Plug_One") throw new Exception("Selected zone IDs did not survive catalog refresh/export.");
        }
        // ThemeMode's code API is experimental; confine it to this preview test.
#pragma warning disable WPF0001
        foreach (var theme in new[] { ThemeMode.Light, ThemeMode.Dark }) {
        ThemeMode = theme;
        foreach (bool minimum in new[] { false, true }) {
            Width = minimum ? MinWidth : 1040; Height = minimum ? MinHeight : 820;
            foreach (string page in new[] { "general", "coyote", "opossum", "debug", "about" }) {
                _vm.SelectPage(page); await Dispatcher.InvokeAsync(() => UpdateLayout(), DispatcherPriority.Render);
                await Task.Delay(200);
                ValidateLayout(PageArea);
                var bitmap = new RenderTargetBitmap((int)ActualWidth, (int)ActualHeight, 96, 96, PixelFormats.Pbgra32);
                bitmap.Render(this);
                var encoder = new PngBitmapEncoder(); encoder.Frames.Add(BitmapFrame.Create(bitmap));
                using var file = File.Create(System.IO.Path.Combine(_smokeDir!, (theme == ThemeMode.Dark ? "dark-" : "light-") + page + (minimum ? "-minimum" : "") + ".png")); encoder.Save(file);
                if (page == "coyote" && minimum) await ValidateZoneDropdownsAsync();
                if (page is "coyote" or "opossum") {
                    foreach (var expander in VisualDescendants(PageArea).OfType<Expander>()) expander.IsExpanded = true;
                    await Dispatcher.InvokeAsync(() => UpdateLayout(), DispatcherPriority.Render);
                    ValidateLayout(PageArea);
                    foreach (var expander in VisualDescendants(PageArea).OfType<Expander>()) expander.IsExpanded = false;
                }
            }
        }
        }
        ThemeMode = ThemeMode.System;
#pragma warning restore WPF0001
        // Exercise live persistence without terminating the backend session.
        _vm.Devices[0].Channels[0].SocketEnabled = false;
        await _vm.SaveAsync();
        _vm.StopCommand.Execute(null);
        await Task.Delay(300); await _vm.PollAsync();
        if (!_vm.Status.Contains("已急停")) throw new Exception("独立急停事件未生效。");
        await _vm.DeviceActionAsync("resume");
        File.WriteAllText(System.IO.Path.Combine(_smokeDir!, "result.txt"), "PASS: native window frame, Fluent light/dark themes, 5 pages at default/minimum sizes, expandable custom parameters, scan status isolation, Socket/Plug catalogs and selection persistence/export, 11 copyright links, 4 channel defaults, named-pipe backend, service startup, hot save, stop/resume.\n");
        if (_previewOnly) {
            _vm.Devices[0].Channels[0].SocketEnabled = true;
            _vm.Background = false;
            _vm.SelectPage("general");
            _vm.Status = "界面预览 · 使用隔离配置，未连接设备";
            Width = 1120; Height = 860;
            _timer.Start();
            return;
        }
        await FinishExitAsync(0);
    }
}
