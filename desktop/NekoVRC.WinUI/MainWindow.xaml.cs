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
    private readonly Microsoft.UI.Xaml.DispatcherTimer _timer = new() { Interval = TimeSpan.FromMilliseconds(150) };
    private bool _loaded, _polling, _exiting, _allowClose;
    private Task? _exitTask;
    private NavigationTransitionInfo? _lastPageTransition;
    private const int MinimumWidth = 880, MinimumHeight = 640;
    private const int DefaultWidth = 940, DefaultHeight = 640;
    private bool Preview => App.Arguments.FirstOrDefault() == "--preview-ui";
    public MainViewModel ViewModel { get; }

    public MainWindow()
    {
        InitializeComponent();
        HeaderBreadcrumb.ItemsSource = new[] { "基本设置" };
        ViewModel = new(_client); Root.DataContext = ViewModel;
        ViewModel.PropertyChanged += (_, args) => { if (args.PropertyName == nameof(MainViewModel.Theme)) ApplyTheme(); };
        ApplyTheme();
        ViewModel.QrReady += ShowQr;
        ViewModel.WaveformsRequested += OpenWaveforms;
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

    private void BreadcrumbLayoutUpdated(object? sender, object e)
    {
        // Stock WinUI uses a nested Button for ancestors but a single presenter
        // for the current item. Their 1/3 DIP insets round at different nesting
        // levels (notably 125% DPI). Keep the native template/glyph/focus states,
        // but give both paths the same unpadded, fixed-height text box.
        foreach (var item in Descendants(HeaderBreadcrumb).OfType<BreadcrumbBarItem>()) {
            if (item.Height != 40) item.Height = 40;
            foreach (var part in Descendants(item).OfType<FrameworkElement>()) {
                if (part is Button button && part.Name == "PART_ItemButton") {
                    if (button.Padding != new Thickness(0)) button.Padding = new Thickness(0);
                    if (button.Height != 40) button.Height = 40;
                }
                if (part is ContentPresenter presenter && part.Name == "PART_LastItemContentPresenter") {
                    if (presenter.Padding != new Thickness(0)) presenter.Padding = new Thickness(0);
                    if (presenter.Height != 40) presenter.Height = 40;
                }
            }
        }
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
    private void OpenWaveforms()
    {
        Navigation.SelectedItem = Navigation.MenuItems[0];
        ViewModel.SelectPage("waveforms");
        PageTitle.Text = "波形库";
        PageTitle.Visibility = Visibility.Collapsed;
        HeaderBreadcrumb.ItemsSource = new[] { "基本设置", "波形库" };
        HeaderBreadcrumb.Visibility = Visibility.Visible;
        ContentFrame.Navigate(typeof(WaveformsPage), ViewModel, new SlideNavigationTransitionInfo { Effect = SlideNavigationTransitionEffect.FromRight });
    }
    private void BreadcrumbClicked(BreadcrumbBar sender, BreadcrumbBarItemClickedEventArgs args) => NavigateBreadcrumb(args.Index);
    private void NavigateBreadcrumb(int index)
    {
        if (index != 0 || ContentFrame.Content is not WaveformsPage) return;
        ViewModel.SelectPage("general");
        PageTitle.Text = "基本设置";
        HeaderBreadcrumb.ItemsSource = new[] { "基本设置" };
        ContentFrame.Navigate(typeof(GeneralPage), ViewModel, new SlideNavigationTransitionInfo { Effect = SlideNavigationTransitionEffect.FromLeft });
        ContentFrame.BackStack.Clear();
    }
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
        HeaderBreadcrumb.ItemsSource = new[] { PageTitle.Text };
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
        try {
            _client.EmergencyStop();
            try { await ViewModel.FlushSettingsAsync(); } catch (Exception) { /* Keep last valid settings on exit. */ }
            await _client.DisposeAsync();
        }
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
            var strengthSlider = (Controls.StrengthSlider)slider;
            var meter = strengthSlider.LevelBar ?? throw new Exception("Native Slider meter was not attached.");
            var source = Descendants(ContentFrame).OfType<TextBlock>().Single(t => ReferenceEquals(t.DataContext, channel) && Microsoft.UI.Xaml.Automation.AutomationProperties.GetName(t) == "当前触发来源");
            if (strengthSlider.CalculatedStrength != 35 || meter.IsHitTestVisible || !source.Text.Contains("计算 35")) throw new Exception("Computed-strength meter/source label use waveform samples instead of the calculated value.");
            var nativeTrack = Descendants(slider).OfType<Microsoft.UI.Xaml.Shapes.Rectangle>().Single(r => r.Name == "HorizontalTrackRect");
            var nativeFill = Descendants(slider).OfType<Microsoft.UI.Xaml.Shapes.Rectangle>().Single(r => r.Name == "HorizontalDecreaseRect");
            var nativeThumb = Descendants(slider).OfType<Microsoft.UI.Xaml.Controls.Primitives.Thumb>().Single(t => t.Name == "HorizontalThumb");
            // Layout rounding at 125% DPI turns 18 DIP into 23 physical pixels
            // (18.4 DIP). Verify the native style, not exact rounded ActualSize.
            double layoutPixel = 1 / Root.XamlRoot.RasterizationScale;
            if (nativeThumb.Width != 18 || nativeThumb.Height != 18 || Math.Abs(nativeThumb.ActualWidth - 18) > layoutPixel || Math.Abs(nativeThumb.ActualHeight - 18) > layoutPixel) throw new Exception($"Native Slider thumb style/rounded size is wrong: {nativeThumb.ActualWidth} x {nativeThumb.ActualHeight}.");
            if (meter.Height != 6 || meter.ActualHeight <= nativeTrack.ActualHeight || !ReferenceEquals(meter.Background, nativeFill.Fill)) throw new Exception("Level must be thicker than the native track and reuse its official accent brush.");
            if (Math.Abs(meter.ActualWidth - (nativeThumb.ActualWidth / 2 + (slider.ActualWidth - nativeThumb.ActualWidth) * 35 / 200)) > 1) throw new Exception("Level endpoints are not aligned to native Slider thumb travel.");
            strengthSlider.CalculatedStrength = 0;
            if (meter.Visibility != Visibility.Collapsed) throw new Exception("Zero strength must hide the thick level.");
            strengthSlider.CalculatedStrength = 200;
            if (Math.Abs(meter.Width - (slider.ActualWidth - nativeThumb.ActualWidth / 2)) > 1) throw new Exception("Full-scale strength does not reach native Slider endpoint.");
            strengthSlider.CalculatedStrength = 35;
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
            var fallbacks = columns.Children.OfType<Grid>().Single(g => g.Name == "FallbackChoices");
            var extraGroup = columns.Children.OfType<Grid>().Single(g => g.Name == "ExtraChoice");
            var extra = extraGroup.Children.OfType<CheckBox>().Single();
            if (columns.ColumnDefinitions.Count != 3 || Grid.GetColumn(depths) != 0 || Grid.GetColumn(fallbacks) != 1 || Grid.GetColumn(extraGroup) != 2 || Descendants(fallbacks).OfType<CheckBox>().Count() != 2 || extra.Content?.ToString() != "参数 / 地图事件") throw new Exception("Trigger choices must occupy depth/fallback/map columns.");
            var help = Descendants(columns).OfType<Button>().ToArray();
            if (help.Length != 3 || !help.Select(b => ToolTipService.GetToolTip(b)?.ToString()).SequenceEqual(new[] { "启用后始终使用PCS参数参与强度计算，与其他已启用来源取最大值", "当没有可计算深度的插头时或者未处于吸附状态时使用LMS参数计算强度", "使用自定义参数和cheese郊狼地图事件触发" })) throw new Exception("Trigger help icons/tooltips are missing.");
            if (Descendants((Page)ContentFrame.Content).OfType<CommunityToolkit.WinUI.Controls.SettingsCard>().Any(c => ReferenceEquals(c.DataContext, channel) && c.Description != null)) throw new Exception("Channel status hint was not removed.");
            foreach (int row in new[] { 0, 1 }) {
                var depth = depths.Children.OfType<CheckBox>().Single(c => Grid.GetRow(c) == row);
                var zone = depths.Children.OfType<ComboBox>().Single(c => Grid.GetRow(c) == row);
                var depthOrigin = depth.TransformToVisual(columns).TransformPoint(default);
                var zoneOrigin = zone.TransformToVisual(columns).TransformPoint(default);
                if (zone.ActualWidth < 160 || zoneOrigin.X - depthOrigin.X - depth.ActualWidth > 7 || Math.Abs(zoneOrigin.Y + zone.ActualHeight / 2 - depthOrigin.Y - depth.ActualHeight / 2) > 1) throw new Exception("Depth checkbox/zone spacing or row alignment is incorrect.");
                var fallback = fallbacks.Children.OfType<CheckBox>().Single(c => Grid.GetRow(c) == row);
                var icon = fallbacks.Children.OfType<Button>().Single(c => Grid.GetRow(c) == row);
                var checkPoint = fallback.TransformToVisual(columns).TransformPoint(default);
                var iconPoint = icon.TransformToVisual(columns).TransformPoint(default);
                double center = zoneOrigin.Y + zone.ActualHeight / 2;
                if (Math.Abs(checkPoint.Y + fallback.ActualHeight / 2 - center) > .01 || Math.Abs(iconPoint.Y + icon.ActualHeight / 2 - center) > .01) throw new Exception("Fallback/help row centers must match the depth selectors.");
            }
            var iconPositions = fallbacks.Children.OfType<Button>().Select(b => b.TransformToVisual(columns).TransformPoint(default).X).ToArray();
            if (Math.Abs(iconPositions[0] - iconPositions[1]) > .01) throw new Exception("PCS/LMS help columns are not aligned.");
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
        if (Descendants(HeaderActions).OfType<Microsoft.UI.Xaml.Controls.Primitives.ButtonBase>().Count() != 1 || !ReferenceEquals(PauseButton.Command, ViewModel.PauseResumeCommand)) throw new Exception("Header must contain one combined Pause/Resume button.");
        if (_lastPageTransition == null || _lastPageTransition is SuppressNavigationTransitionInfo) throw new Exception("Page animation was disabled.");
        var actionsOrigin = GlobalActions.TransformToVisual(LayoutArea).TransformPoint(default);
        var statusOrigin = ServiceStatus.TransformToVisual(LayoutArea).TransformPoint(default);
        if (Math.Abs(actionsOrigin.X + GlobalActions.ActualWidth - LayoutArea.ActualWidth) > 1 || Math.Abs(actionsOrigin.Y + GlobalActions.ActualHeight / 2 - HeaderBar.ActualHeight / 2) > 1) throw new Exception("Action buttons are not fixed at the header's right edge.");
        if (Math.Abs(statusOrigin.X + ServiceStatus.ActualWidth + 12 - actionsOrigin.X) > 1 || Math.Abs(statusOrigin.Y + ServiceStatus.ActualHeight / 2 - actionsOrigin.Y - GlobalActions.ActualHeight / 2) > 1 || ServiceStatus.TextAlignment != TextAlignment.Right || ServiceStatus.ActualWidth < 80) throw new Exception($"Service/avatar status is not on the left of the same button row: {tag}, origin={statusOrigin}, size={ServiceStatus.ActualWidth}x{ServiceStatus.ActualHeight}, actions={actionsOrigin}, size={GlobalActions.ActualWidth}x{GlobalActions.ActualHeight}.");
        if (HeaderBar.ActualHeight > 41 || ContentFrame.Clip is not RectangleGeometry clip || Math.Abs(clip.Rect.Height - ContentFrame.ActualHeight) > 1) throw new Exception("Compact header or transition viewport is incorrect.");
        if (ContentFrame.BackStack.Count != 0 || ContentFrame.ForwardStack.Count != 0) throw new Exception("Menu navigation retained stale pages.");
    }
    private async Task SmokeAsync()
    {
        await _client.CallAsync("bluetooth_check");
        ViewModel.PrepareSmokePorts(FreePort(), FreePort(), FreePort(), FreePort(true));
        await ViewModel.SaveAsync(); await ViewModel.DeviceActionAsync("start"); await ViewModel.PollAsync();
        if (App.Arguments[0] == "--smoke-waveforms") {
            await VerifySteamVRRollbackAsync(); await VerifyWaveformsAsync();
            File.WriteAllText(Path.Combine(App.TestDirectory!, "result.txt"), "PASS: SteamVR toggle rollback/bindings; native heading alignment/glyph; amplitude and frequency previews; waveform catalog and navigation.\n");
            await FinishExitAsync(); return;
        }
        string runLog = (await _client.CallAsync("snapshot"))["data"]!["log_path"]!.GetValue<string>();
        using (var logStream = new FileStream(runLog, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
        using (var logReader = new StreamReader(logStream, System.Text.Encoding.UTF8)) {
            string fileLog = logReader.ReadToEnd();
            if (!fileLog.Contains("计算强度 device=") || !fileLog.Contains("neko-vrc:") || fileLog.Contains("shocking_vrchat:")) throw new Exception("Per-run log file/brand/computed-strength persistence failed.");
            if (ViewModel.Logs.Contains(runLog) || ViewModel.Logs.Split('\n', StringSplitOptions.RemoveEmptyEntries).Any(line => !line.Contains("计算 ") || !line.Contains("实际 "))) throw new Exception("Debug must show strength values only, not full diagnostic logs.");
        }
        if (MainViewModel.FormatCommunicationLatency(new JsonObject { ["kind"] = "app_ack", ["milliseconds"] = 12.5 }) != "App 确认往返 12.5 ms" ||
            MainViewModel.FormatCommunicationLatency(new JsonObject { ["kind"] = "ble_write_response", ["milliseconds"] = 3.25 }) != "蓝牙写入 3.25 ms" ||
            MainViewModel.FormatCommunicationLatency(new JsonObject { ["kind"] = "ble_write_enqueue", ["milliseconds"] = 1.5 }) != "蓝牙入队 1.5 ms" ||
            MainViewModel.FormatCommunicationLatency(null) != "通信延迟：暂无数据") throw new Exception("Connection latency labels must distinguish measured links and missing data.");
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
        if (ServicePlaybackIcon.Glyph != "\uE769" || PauseButton.IsChecked != true || PauseButton.ActualWidth != 28 || PauseButton.ActualHeight != 24 || ServicePlaybackIcon.FontSize != 12) throw new Exception("Service button must match preview's compact native playback icon style.");
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
        var liveChannels = new JsonObject();
        foreach (string kind in new[] { "coyote", "opossum" }) {
            var channels = new JsonObject();
            foreach (string channel in new[] { "A", "B" }) channels[channel] = new JsonObject {
                ["strength_percentage"] = 0.5, ["calculated_strength"] = 35.0, ["upper_strength"] = 70,
                ["trigger_type"] = channel == "A" ? "sps_socket" : "lms", ["sent_strengths"] = new JsonArray(100, 0, 100, 0),
            };
            liveChannels[kind] = channels;
        }
        var testState = new JsonObject { ["service"] = "running", ["avatar"] = catalog.DeepClone(), ["runtime"] = new JsonObject { ["device_channels"] = liveChannels,
            ["devices"] = new JsonArray(
                new JsonObject { ["device_kind"] = "coyote", ["device_name"] = "测试郊狼", ["transport"] = "bluetooth", ["communication_latency"] = new JsonObject { ["kind"] = "ble_write_response", ["milliseconds"] = 3.25 } },
                new JsonObject { ["device_kind"] = "opossum", ["device_name"] = "测试负鼠", ["transport"] = "app", ["communication_latency"] = new JsonObject { ["kind"] = "app_ack", ["milliseconds"] = 12.5 } }) } };
        ViewModel.UpdateState(testState);
        if (!ViewModel.Devices[0].Status.Contains("蓝牙写入 3.25 ms") || !ViewModel.Devices[1].Status.Contains("App 确认往返 12.5 ms")) throw new Exception("Connection card latencies are missing or mixed between device types.");
        var autoChannel = ViewModel.Devices[0].Channels[0];
        string originalAutoLimit = autoChannel.Limit;
        autoChannel.Limit = "77";
        await ViewModel.PendingApplyTask;
        var autoPersisted = (await _client.CallAsync("init"))["data"]!;
        if (autoPersisted["basic"]!["dglab3"]!["coyote"]!["channel_a"]!["strength_limit"]!.GetValue<int>() != 77) throw new Exception("Limit edit was not automatically applied/persisted.");
        for (int value = 78; value < 84; value++) { autoChannel.LimitValue = value; await Task.Delay(50); }
        autoPersisted = (await _client.CallAsync("init"))["data"]!;
        if (autoPersisted["basic"]!["dglab3"]!["coyote"]!["channel_a"]!["strength_limit"]!.GetValue<int>() < 78) throw new Exception("Continuous dragging was delayed until release instead of throttled live application.");
        await ViewModel.PendingApplyTask;
        autoChannel.Limit = "77";
        await ViewModel.PendingApplyTask;
        autoChannel.Limit = "";
        await ViewModel.PendingApplyTask;
        autoPersisted = (await _client.CallAsync("init"))["data"]!;
        if (autoPersisted["basic"]!["dglab3"]!["coyote"]!["channel_a"]!["strength_limit"]!.GetValue<int>() != 77 || !ErrorBar.IsOpen) throw new Exception("Invalid pending input overwrote the last valid limit.");
        autoChannel.Limit = originalAutoLimit;
        await ViewModel.PendingApplyTask;
        bool originalBackground = ViewModel.Background;
        ViewModel.Background = !originalBackground;
        await ViewModel.PendingApplyTask;
        autoPersisted = (await _client.CallAsync("init"))["data"]!;
        if (autoPersisted["settings"]!["general"]!["run_in_background"]!.GetValue<bool>() == originalBackground) throw new Exception("General setting was not automatically applied.");
        ViewModel.Background = originalBackground;
        await ViewModel.PendingApplyTask;
        ViewModel.QrReady -= ShowQr;
        bool qrRaised = false;
        Action<JsonArray> qrCapture = matrix => qrRaised = matrix.Count >= 21;
        ViewModel.QrReady += qrCapture;
        try {
            ViewModel.Devices[0].HasBluetoothConnection = true;
            await ViewModel.ShowQrAsync();
            if (!qrRaised || ViewModel.Devices[0].Mode != "bluetooth" || ViewModel.Devices[1].Mode != "app") throw new Exception("General QR displaced a connected BLE host or failed to select App routing.");
            await ViewModel.ShowQrAsync("coyote");
            if (ViewModel.Devices[0].Mode != "app") throw new Exception("Device QR did not select its own App route.");
        } finally {
            ViewModel.QrReady -= qrCapture; ViewModel.QrReady += ShowQr;
            foreach (var device in ViewModel.Devices) { device.HasBluetoothConnection = false; device.Mode = "bluetooth"; }
            await ViewModel.FlushSettingsAsync();
        }
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
                        ViewModel.Logs = string.Join('\n', Enumerable.Range(0, 150).Select(i => $"21:00:00 郊狼 A：计算 {i} / 实际 100"));
                        await Task.Delay(60);
                        var logScroll = Descendants(log).OfType<ScrollViewer>().First(s => s.ScrollableHeight > 0);
                        if (Math.Abs(logScroll.VerticalOffset - logScroll.ScrollableHeight) > 1) throw new Exception("Debug log did not automatically follow the latest line.");
                    }
                    await CaptureAsync($"{theme.ToString().ToLowerInvariant()}-{item.Tag}{suffix}");
                }
            }
        }
        ViewModel.Theme = "system";
        Root.Background = null;
        await VerifySteamVRRollbackAsync();
        await VerifyWaveformsAsync();
        var changedChannel = ViewModel.Devices[0].Channels[0]; changedChannel.SocketEnabled = false;
        ViewModel.Theme = "dark";
        await ViewModel.SaveAsync();
        var persisted = (await _client.CallAsync("init"))["data"]!;
        if (persisted["settings"]!["general"]!["theme"]!.GetValue<string>() != "dark") throw new Exception("Theme setting was not persisted.");
        if (persisted["basic"]!["dglab3"]!["coyote"]!["channel_a"]!["socket_enabled"]!.GetValue<bool>()) throw new Exception("Hot save did not persist.");
        var busyOperation = new TaskCompletionSource();
        ViewModel.Run(() => busyOperation.Task);
        if (!ViewModel.Busy || !PauseButton.IsEnabled) throw new Exception("Pause must remain available during other backend operations.");
        PauseButton.Command.Execute(null);
        if (ViewModel.OutputPaused || ServicePlaybackIcon.Glyph != "\uE769") throw new Exception("Pause icon must wait for confirmed backend state.");
        busyOperation.SetResult(); await Task.Delay(300); await ViewModel.PollAsync();
        if (!ViewModel.Status.Contains("已暂停") || !ViewModel.OutputPaused || ServicePlaybackIcon.Glyph != "\uE768" || PauseButton.IsChecked != false || Microsoft.UI.Xaml.Automation.AutomationProperties.GetName(PauseButton) != "恢复") throw new Exception("Combined pause button did not confirm independent pause.");
        PauseButton.Command.Execute(null);
        for (int i = 0; i < 200 && ViewModel.Busy; i++) await Task.Delay(10);
        await ViewModel.PollAsync();
        if (ViewModel.OutputPaused || ServicePlaybackIcon.Glyph != "\uE769" || PauseButton.IsChecked != true || ViewModel.Status != "服务运行中") throw new Exception("The same button did not resume service.");
        string result = "PASS: waveform chevron entry and parent breadcrumbs; animated histogram, preview pause/resume and timer cleanup; import/deduplication, catalog reorder, device isolation, live custom selection, both themes; no Save button; automatic coalesced settings application/persistence and invalid-input protection; scoped QR selects App without displacing connected BLE hosts; computed-strength meter/source labels are independent of waveform samples; default 940x640 compact layout; global hints and help; themes, five pages and three sizes; 24 rapid switches preserve settings; pause/resume and immediate-hide shutdown.\n";
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

    private async Task VerifyWaveformsAsync()
    {
        static bool Shown(DependencyObject node) {
            for (DependencyObject? current = node; current != null; current = VisualTreeHelper.GetParent(current))
                if (current is UIElement element && element.Visibility != Visibility.Visible) return false;
            return true;
        }
        Navigation.SelectedItem = Navigation.MenuItems[0];
        await Task.Delay(100); Root.UpdateLayout();
        // RenderTargetBitmap cannot capture the DWM Mica surface.
        Root.Background = new SolidColorBrush(Root.ActualTheme == ElementTheme.Dark ? Windows.UI.Color.FromArgb(255, 32, 32, 32) : Windows.UI.Color.FromArgb(255, 243, 243, 243));
        var entry = Descendants((Page)ContentFrame.Content).OfType<CommunityToolkit.WinUI.Controls.SettingsCard>().Single(card => Equals(card.Header, "波形库"));
        if (!entry.IsClickEnabled || !entry.IsActionIconVisible || entry.Content != null || !ReferenceEquals(entry.Command, ViewModel.WaveformsCommand)) throw new Exception("Waveform entry must be a clickable settings card with a right chevron, not a nested button.");
        var rootHeading = Descendants(HeaderBreadcrumb).OfType<TextBlock>().Single(t => t.Text == "基本设置" && t.ActualWidth > 0 && Shown(t));
        var rootPosition = rootHeading.TransformToVisual(Root).TransformPoint(default);
        double rootBaseline = rootPosition.Y + rootHeading.BaselineOffset;
        OpenWaveforms(); await Task.Delay(100);
        var page = (WaveformsPage)ContentFrame.Content;
        await page.LoadTask;
        Root.UpdateLayout();
        var parentHeading = Descendants(HeaderBreadcrumb).OfType<TextBlock>().Single(t => t.Text == "基本设置" && t.ActualWidth > 0 && Shown(t));
        var parentPosition = parentHeading.TransformToVisual(Root).TransformPoint(default);
        const double layoutPixel = .01; // No visible pixel shift, even at fractional DPI.
        if (Math.Abs(parentPosition.X - rootPosition.X) > layoutPixel || Math.Abs(parentPosition.Y + parentHeading.BaselineOffset - rootBaseline) > layoutPixel || HeaderBreadcrumb.ItemTemplate != null) throw new Exception($"Native root/parent heading alignment changed: {rootPosition} vs {parentPosition}.");
        await CaptureAsync("heading-native");
        var parentItem = Descendants(HeaderBreadcrumb).OfType<BreadcrumbBarItem>().Single(item => Equals(item.Content, "基本设置"));
        var chevron = Descendants(parentItem).OfType<TextBlock>().Single(t => t.Name == "PART_ChevronTextBlock");
        if (chevron.Text != "\uE974" || chevron.FontSize != 20 || parentHeading.FontSize != 28) throw new Exception("Breadcrumb heading must use official native glyph and typography resources.");
        if (AppTitleBar.IsBackButtonVisible || PageTitle.Visibility != Visibility.Collapsed || HeaderBreadcrumb.Visibility != Visibility.Visible || !((System.Collections.Generic.IEnumerable<string>)HeaderBreadcrumb.ItemsSource).SequenceEqual(new[] { "基本设置", "波形库" })) throw new Exception("Waveform subpage must use parent-title breadcrumbs without a caption back button.");
        NavigateBreadcrumb(1);
        if (!ReferenceEquals(page, ContentFrame.Content)) throw new Exception("Clicking the current breadcrumb should not reload the page.");
        var builtinList = Descendants(page).OfType<ListView>().Single(l => l.Name == "EnabledList");
        var fast = WaveformsPage.PreviewGeometry(new byte[] { 25 }, WaveformsPage.BuildPhase(new byte[] { 10 }), 2000, 600, 100);
        var slow = WaveformsPage.PreviewGeometry(new byte[] { 100 }, WaveformsPage.BuildPhase(new byte[] { 240 }), 2000, 600, 100);
        if (fast.Count != 200 || slow.Count != 2 || fast.Any(b => Math.Abs(b.Height - 25) > .01) || slow.Any(b => Math.Abs(b.Height - 100) > .01) || Math.Abs(fast[1].X - fast[0].X - 3) > .01 || Math.Abs(slow[1].X - slow[0].X - 300) > .01) throw new Exception("Combined preview must encode amplitude in height and physical frequency in spacing.");
        foreach (double time in new[] { 0.0, .1, 125.5, 2001.0, 3500123.0 }) {
            var wrapped = WaveformsPage.PreviewGeometry(new byte[] { 25 }, WaveformsPage.BuildPhase(new byte[] { 10 }), time, 400, 100);
            if (wrapped.Count != 200 || wrapped.Any(b => b.X < 0 || b.X + b.Width > 400.01 || b.Height != 25)) throw new Exception("Looped/negative preview phase or clipping is incorrect.");
        }
        var changing = WaveformsPage.PreviewGeometry(new byte[] { 25, 100 }, WaveformsPage.BuildPhase(new byte[] { 10, 240 }), 2000, 600, 100);
        if (Math.Abs(changing[3].X - 16.425) > .0001) throw new Exception("Frequency changes reset pulse phase instead of integrating it.");
        var envelope = WaveformsPage.PreviewGeometry(new byte[] { 25, 100 }, Array.Empty<double>(), 2000, 600, 100);
        if (envelope.Count != 80 || envelope[0].Height != 25 || envelope[1].Height != 100) throw new Exception("Opossum envelope must retain 25 ms amplitudes without a fake frequency.");
        foreach (var (name, suffix, changes) in new[] { ("脉冲", "pulse", true), ("压缩", "compress", true), ("树荫", "shade", false) }) {
            builtinList.SelectedItem = builtinList.Items.OfType<WaveformItem>().Single(wave => wave.Name == name);
            await Task.Delay(450); page.SetPreviewPlayback(false); Root.UpdateLayout();
            var preview = await ViewModel.WaveformOperationAsync("waveform_preview", new() { ["kind"] = "coyote", ["id"] = ((WaveformItem)builtinList.SelectedItem).Id });
            var phase = WaveformsPage.BuildPhase(Convert.FromHexString(preview["frequency_hex"]!.GetValue<string>()));
            var rates = phase.Zip(phase.Skip(1), (a, b) => b - a).ToArray();
            var bars = Descendants(page).OfType<Canvas>().Single(c => c.Name == "PreviewCanvas").Children.OfType<Microsoft.UI.Xaml.Shapes.Rectangle>().Where(b => b.Visibility == Visibility.Visible).ToArray();
            if (Descendants(page).OfType<Canvas>().Count() != 1 || bars.Length == 0 || bars.Any(b => b.Height <= 0) || bars.Max(b => b.Height) - bars.Min(b => b.Height) > .01 || (rates.Max() - rates.Min() > .01) != changes) throw new Exception("Constant amplitude/frequency must share one truthful pulse plot: " + name);
            await CaptureAsync("waveform-frequency-" + suffix);
        }
        if (WaveformsPage.FrequencyHz(10) != 100 || WaveformsPage.FrequencyHz(100) != 10 || WaveformsPage.FrequencyHz(200) != 1000.0 / 600 || WaveformsPage.FrequencyHz(240) != 1) throw new Exception("Coyote frequency bytes were not decoded to real Hz.");
        string pulsePath = Path.Combine(App.TestDirectory!, "smoke.pulse");
        File.WriteAllText(pulsePath, "Dungeonlab+pulse:0,1,16=0,83,3,1,1/0-1,100-1");
        await page.ImportPathsAsync(new[] { pulsePath });
        await page.ImportPathsAsync(new[] { pulsePath });
        var lists = Descendants(page).OfType<ListView>().ToArray();
        var library = lists.Single(l => l.Name == "LibraryList");
        var enabled = lists.Single(l => l.Name == "EnabledList");
        var imported = library.Items.OfType<WaveformItem>().Single(i => !i.Builtin);
        library.SelectedItem = imported;
        await Task.Delay(150);
        var histogram = Descendants(page).OfType<Canvas>().Single(c => c.Name == "PreviewCanvas");
        if (!histogram.Children.OfType<Microsoft.UI.Xaml.Shapes.Rectangle>().Any(bar => bar.Height > 0) || !page.IsPreviewAnimating) throw new Exception("Dynamic waveform histogram did not render/start.");
        int frame = page.PreviewFrameCount; await Task.Delay(100);
        if (page.PreviewFrameCount <= frame) throw new Exception("Waveform histogram is static instead of animated.");
        page.SetPreviewPlayback(false); frame = page.PreviewFrameCount; await Task.Delay(100);
        if (page.IsPreviewAnimating || page.PreviewFrameCount != frame) throw new Exception("Pause preview did not freeze the visual clock.");
        page.SetPreviewPlayback(true); await Task.Delay(100);
        if (!page.IsPreviewAnimating || page.PreviewFrameCount <= frame) throw new Exception("Preview did not resume.");
        await page.MoveSelectionAsync(new[] { imported.Id }.ToHashSet(), true);
        if (!enabled.Items.OfType<WaveformItem>().Any(i => i.Id == imported.Id) || library.Items.OfType<WaveformItem>().Any(i => i.Id == imported.Id)) throw new Exception("Cross-list waveform move failed.");
        await page.MoveSelectionAsync(new[] { imported.Id }.ToHashSet(), false);
        if (enabled.Items.OfType<WaveformItem>().Any(i => i.Id == imported.Id)) throw new Exception("Waveform removal failed.");
        var listIds = enabled.Items.OfType<WaveformItem>().Select(i => i.Id).Prepend(imported.Id).ToArray();
        var result = await ViewModel.WaveformOperationAsync("waveform_select", new() { ["kind"] = "coyote", ["enabled"] = new JsonArray(listIds.Select(id => (JsonNode?)JsonValue.Create(id)).ToArray()) });
        var coyote = ViewModel.Devices[0].Channels[0];
        var opossum = ViewModel.Devices[1].Channels[0];
        if (coyote.WaveformChoices[0].Name != imported.Name || opossum.Waveforms.Contains(imported.Id)) throw new Exception("Waveform library display names/isolation failed.");
        coyote.Waveform = imported.Id; await ViewModel.PendingApplyTask;
        Navigation.SelectedItem = Navigation.MenuItems[1]; await Task.Delay(150); Root.UpdateLayout();
        if (page.IsPreviewAnimating) throw new Exception("Outgoing waveform page retained its animation timer.");
        var selector = Descendants(ContentFrame).OfType<ComboBox>().Single(b => ReferenceEquals(b.ItemsSource, coyote.WaveformChoices));
        if (selector.SelectedValue as string != imported.Id || (selector.SelectedItem as Zone)?.Name != imported.Name) throw new Exception("Imported waveform selection did not survive page navigation.");
        var saved = (await _client.CallAsync("init"))["data"]!;
        if (saved["settings"]!["dglab3"]!["coyote"]!["channel_a"]!["depth"]!["waveform"]!.GetValue<string>() != imported.Id) throw new Exception("Imported waveform was not hot-applied.");
        coyote.Waveform = "纯粹"; await ViewModel.PendingApplyTask;
        OpenWaveforms(); await Task.Delay(100); page = (WaveformsPage)ContentFrame.Content; await page.LoadTask;
        var kindBox = Descendants(page).OfType<ComboBox>().Single(b => b.Name == "KindBox");
        var originalSize = AppWindow.Size;
        double scale = Root.XamlRoot.RasterizationScale;
        foreach (int width in new[] { 880, 940, 1120 }) {
            AppWindow.Resize(new SizeInt32((int)Math.Ceiling(width * scale), originalSize.Height));
            await Task.Delay(100); Root.UpdateLayout();
            foreach (string prefix in new[] { "Library", "Enabled" }) {
                var heading = Descendants(page).OfType<TextBlock>().Single(t => t.Name == prefix + "Heading");
                var container = Descendants(page).OfType<Border>().Single(b => b.Name == prefix + "Container");
                if (!ReferenceEquals(VisualTreeHelper.GetParent(heading), VisualTreeHelper.GetParent(container)) || Math.Abs(heading.TransformToVisual(page).TransformPoint(default).X - container.TransformToVisual(page).TransformPoint(default).X) > .01) throw new Exception("Waveform heading and sortable list must share an exact column: " + prefix);
            }
        }
        AppWindow.Resize(originalSize); await Task.Delay(100); Root.UpdateLayout();
        foreach (string theme in new[] { "light", "dark" }) {
            ViewModel.Theme = theme;
            Root.Background = new SolidColorBrush(theme == "light" ? Windows.UI.Color.FromArgb(255, 243, 243, 243) : Windows.UI.Color.FromArgb(255, 32, 32, 32));
            foreach (int index in new[] { 0, 1 }) {
                kindBox.SelectedIndex = index; await Task.Delay(450); Root.UpdateLayout();
                Descendants(page).OfType<ListView>().Single(l => l.Name == "EnabledList").SelectedIndex = 0;
                await Task.Delay(150); Root.UpdateLayout();
                if (index == 1 && Descendants(page).OfType<ListView>().SelectMany(l => l.Items.OfType<WaveformItem>()).Any(i => i.Id == imported.Id)) throw new Exception("Coyote waveform leaked into Opossum library.");
                var previewCard = Descendants(page).OfType<Border>().Single(b => b.Name == "PreviewCard");
                var previewTitle = Descendants(previewCard).OfType<TextBlock>().Single(t => t.Name == "PreviewTitle");
                var selectedWave = (WaveformItem)Descendants(page).OfType<ListView>().Single(l => l.Name == "EnabledList").SelectedItem;
                if (previewTitle.Text != selectedWave.Name || Descendants(previewCard).OfType<TextBlock>().Any(t => t != previewTitle && !string.IsNullOrEmpty(t.Text) && !InIcon(t)) || Descendants(previewCard).Any(t => ToolTipService.GetToolTip(t) != null)) throw new Exception("Waveform preview must show only the waveform name, chart and icon control.");
                if (page.ActualWidth > ContentFrame.ActualWidth + 1 || Descendants(page).OfType<ListView>().Any(l => l.ActualHeight < 100)) throw new Exception("Waveform layout does not fit minimum window dimensions.");
                await CaptureAsync($"{theme}-waveforms-{index}");
            }
        }
        Root.Background = null; ViewModel.Theme = "system";
        NavigateBreadcrumb(0);
        if (ContentFrame.Content is not GeneralPage || AppTitleBar.IsBackButtonVisible || HeaderBreadcrumb.Visibility != Visibility.Visible || PageTitle.Visibility != Visibility.Collapsed || page.IsPreviewAnimating) throw new Exception("Parent breadcrumb navigation or animation cleanup failed.");

        static bool InIcon(DependencyObject node) {
            for (DependencyObject? current = node; current != null; current = VisualTreeHelper.GetParent(current))
                if (current is IconElement) return true;
            return false;
        }
    }

    private async Task VerifySteamVRRollbackAsync()
    {
        var initial = (JsonObject)(await _client.CallAsync("init")).DeepClone();
        initial["data"]!["settings"]!["general"]!["steamvr_auto_start"] = false;
        int saves = 0; string lastError = "";
        var isolated = new MainViewModel(_client, (method, data) => {
            if (method == "init") return Task.FromResult((JsonObject)initial.DeepClone());
            if (method == "save") {
                saves++;
                if (data!["settings"]!["general"]!["steamvr_auto_start"]!.GetValue<bool>()) return Task.FromException<JsonObject>(new IOException("模拟 SteamVR 保存失败"));
            }
            return Task.FromResult(new JsonObject());
        });
        isolated.OperationError += message => lastError = message;
        await isolated.InitializeAsync();
        Navigation.SelectedItem = Navigation.MenuItems[0]; await Task.Delay(100); Root.UpdateLayout();
        var page = (Page)ContentFrame.Content; var previousContext = page.DataContext;
        try {
            page.DataContext = isolated; await Task.Delay(50); Root.UpdateLayout();
            var toggle = Descendants(page).OfType<ToggleSwitch>().Single(t => Microsoft.UI.Xaml.Automation.AutomationProperties.GetName(t) == "跟随 SteamVR 启动");
            toggle.IsOn = true; await isolated.PendingApplyTask; await Task.Delay(150);
            if (toggle.IsOn || isolated.SteamVR || saves != 1 || !lastError.Contains("保存失败")) throw new Exception($"SteamVR failed enable must revert the visible toggle without retrying: toggle={toggle.IsOn}, model={isolated.SteamVR}, saves={saves}, error={lastError}, bound={ReferenceEquals(toggle.DataContext, isolated)}.");
            isolated.Background = !isolated.Background; await isolated.PendingApplyTask;
            if (isolated.SteamVR || saves != 2) throw new Exception("A later unrelated edit retried the rejected SteamVR enable.");
            var channel = isolated.Devices[0].Channels[0]; string limit = channel.Limit;
            channel.Limit = ""; toggle.IsOn = true; await isolated.PendingApplyTask;
            if (toggle.IsOn || isolated.SteamVR) throw new Exception("Local validation failure did not revert the SteamVR toggle.");
            channel.Limit = limit; await isolated.PendingApplyTask;
        } finally { page.DataContext = previousContext; Root.UpdateLayout(); }
    }
}
