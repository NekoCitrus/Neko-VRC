using System;
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
using System.Windows.Shell;
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
    public MainWindow(string[] args)
    {
        InitializeComponent();
        _smokeDir = args.Length == 2 && args[0] == "--smoke-test" ? System.IO.Path.GetFullPath(args[1]) : null;
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
            try { await _vm.PollAsync(); }
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
    private void MinimizeWindow(object sender, RoutedEventArgs e) => WindowState = WindowState.Minimized;
    private void ToggleMaximize(object sender, RoutedEventArgs e) => WindowState = WindowState == WindowState.Maximized ? WindowState.Normal : WindowState.Maximized;
    private void CloseWindow(object sender, RoutedEventArgs e) => Close();
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
            if (child is TextBox || child is ComboBox || child is Button || child is CheckBox || child is TextBlock) continue;
            if (child is ScrollViewer) throw new Exception("Unexpected page scrollbar.");
            ValidateLayout(child);
        }
    }

    private async Task SmokeAsync()
    {
        await _client.CallAsync("bluetooth_check");
        _vm.PrepareSmokePorts(FreePort(), FreePort(), FreePort(), FreePort(true));
        await _vm.SaveAsync(); await _vm.DeviceActionAsync("start"); await _vm.PollAsync();
        if (_vm.Devices.Count != 2 || _vm.Devices.Any(d => d.Channels.Count != 2)) throw new Exception("通道数量错误。");
        foreach (var device in _vm.Devices) foreach (var channel in device.Channels) {
            if (!channel.SocketEnabled || !channel.PlugEnabled || !channel.ExtraEnabled ||
                channel.PcsEnabled != (device.Kind == "coyote") || channel.LmsEnabled != (device.Kind == "opossum")) throw new Exception("默认开关错误。");
        }
        if (((SolidColorBrush)Foreground).Color != Color.FromRgb(250, 230, 135) || ((SolidColorBrush)Background).Color != Colors.Black) throw new Exception("主题颜色错误。");
        if (WindowStyle != WindowStyle.None || WindowChrome.GetWindowChrome(this)?.CaptionHeight != 32) throw new Exception("Unexpected native title bar.");
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
        _vm.UpdateState(new JsonObject { ["service"] = "running", ["runtime"] = new JsonObject { ["device_channels"] = channels } });
        if (!_vm.Devices.SelectMany(d => d.Channels).Select(c => c.ParameterHint).SequenceEqual(hints)) throw new Exception("Parameter hints were not bound to the correct channels.");
        foreach (bool minimum in new[] { false, true }) {
            Width = minimum ? MinWidth : 1040; Height = minimum ? MinHeight : 820;
            foreach (string page in new[] { "general", "coyote", "opossum", "debug", "about" }) {
                _vm.SelectPage(page); await Dispatcher.InvokeAsync(() => UpdateLayout(), DispatcherPriority.Render);
                await Task.Delay(200);
                ValidateLayout(PageArea);
                var bitmap = new RenderTargetBitmap((int)ActualWidth, (int)ActualHeight, 96, 96, PixelFormats.Pbgra32);
                bitmap.Render(this);
                var encoder = new PngBitmapEncoder(); encoder.Frames.Add(BitmapFrame.Create(bitmap));
                using var file = File.Create(System.IO.Path.Combine(_smokeDir!, page + (minimum ? "-minimum" : "") + ".png")); encoder.Save(file);
            }
        }
        // Exercise live persistence without terminating the backend session.
        _vm.Devices[0].Channels[0].SocketEnabled = false;
        await _vm.SaveAsync();
        _vm.StopCommand.Execute(null);
        await Task.Delay(300); await _vm.PollAsync();
        if (!_vm.Status.Contains("已急停")) throw new Exception("独立急停事件未生效。");
        await _vm.DeviceActionAsync("resume");
        File.WriteAllText(System.IO.Path.Combine(_smokeDir!, "result.txt"), "PASS: WPF theme/title bar, 5 pages at default/minimum sizes without clipping or outer scrollbars, parameter-hint channel binding, 11 copyright links, 4 channel defaults, named-pipe backend, service startup, hot save, stop/resume.\n");
        await FinishExitAsync(0);
    }
}
