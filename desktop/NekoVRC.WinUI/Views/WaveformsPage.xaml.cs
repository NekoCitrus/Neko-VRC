using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Diagnostics;
using System.Linq;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Navigation;
using Microsoft.UI.Xaml.Shapes;
using Windows.ApplicationModel.DataTransfer;
using Windows.Storage.Pickers;

namespace NekoVRC.WinUI.Views;

public sealed record WaveformItem(string Id, string Name, bool Builtin, double Duration)
{
    public string Detail => $"{(Builtin ? "内置" : "导入")} · {Duration:0.###} 秒";
}

public sealed partial class WaveformsPage : Page
{
    private const string DragPrefix = "NekoVRC-waveform:";
    private MainViewModel? _model;
    private JsonObject _catalog = new();
    private readonly ObservableCollection<WaveformItem> _library = new(), _enabled = new();
    private byte[] _preview = Array.Empty<byte>();
    private byte[] _previewFrequency = Array.Empty<byte>();
    private double[] _previewPhase = Array.Empty<double>();
    private readonly List<Rectangle> _histogramBars = new();
    private readonly Stopwatch _previewClock = new();
    private readonly DispatcherTimer _previewTimer = new() { Interval = TimeSpan.FromMilliseconds(33) };
    internal bool IsPreviewAnimating => _previewTimer.IsEnabled;
    internal int PreviewFrameCount { get; private set; }
    private int _previewVersion;
    private bool _updating, _working, _active;
    public Task LoadTask { get; private set; } = Task.CompletedTask;
    private string Kind => (KindBox.SelectedItem as ComboBoxItem)?.Tag?.ToString() ?? "coyote";

    public WaveformsPage()
    {
        InitializeComponent();
        LibraryList.ItemsSource = _library; EnabledList.ItemsSource = _enabled;
        _previewTimer.Tick += PreviewTick;
        Loaded += (_, _) => { if (_active && _preview.Length > 0) SetPreviewPlayback(PreviewPlayback.IsChecked == true); };
        Unloaded += (_, _) => StopPreviewTimer();
    }

    protected override void OnNavigatedTo(NavigationEventArgs e)
    {
        base.OnNavigatedTo(e);
        _model = (MainViewModel)e.Parameter; _active = true;
        KindBox.SelectedIndex = 0;
        LoadTask = Perform(async () => { var data = await _model.WaveformOperationAsync("waveforms"); ApplyCatalog(data["catalog"]!.AsObject()); });
    }

    protected override void OnNavigatedFrom(NavigationEventArgs e)
    {
        _active = false; _previewVersion++; StopPreviewTimer();
        base.OnNavigatedFrom(e);
    }

    private async Task Perform(Func<Task> action)
    {
        if (_working) return;
        _working = true;
        KindBox.IsEnabled = ImportFileButton.IsEnabled = ImportFolderButton.IsEnabled = false;
        LibraryList.CanDragItems = EnabledList.CanDragItems = EnabledList.CanReorderItems = false;
        try { await action(); }
        catch (Exception exc) { Notice.Severity = InfoBarSeverity.Error; Notice.Message = exc.Message; Notice.IsOpen = true; }
        finally {
            _working = false;
            KindBox.IsEnabled = ImportFileButton.IsEnabled = ImportFolderButton.IsEnabled = true;
            LibraryList.CanDragItems = EnabledList.CanDragItems = EnabledList.CanReorderItems = true;
        }
    }

    private void ApplyCatalog(JsonObject catalog)
    {
        _catalog = (JsonObject)catalog.DeepClone(); Populate();
    }

    private void Populate()
    {
        if (_catalog[Kind] is not JsonObject section) return;
        _updating = true; _previewVersion++;
        var items = section["items"]!.AsArray().Select(n => new WaveformItem(n!["id"]!.GetValue<string>(), n["name"]!.GetValue<string>(), n["builtin"]!.GetValue<bool>(), n["duration"]!.GetValue<double>())).ToDictionary(p => p.Id);
        var enabled = section["enabled"]!.AsArray().Select(n => n!.GetValue<string>()).ToArray();
        _library.Clear(); _enabled.Clear();
        foreach (var item in items.Values.Where(p => !enabled.Contains(p.Id))) _library.Add(item);
        foreach (string id in enabled) _enabled.Add(items[id]);
        _updating = false;
        FormatHint.Text = Kind == "coyote" ? "支持郊狼 .pulse、ZIP 或文件夹，拖到右侧加入选择框，拖回左侧移出，Ctrl / Shift 多选，拖动排序" : "负鼠库独立保存。仅导入标记为 opossum 的帧文件；郊狼 .pulse 不通用。";
        ClearPreview();
    }

    private void KindChanged(object sender, SelectionChangedEventArgs e) { if (_model != null && !_working) Populate(); }

    private static void InitializePicker(object picker) => WinRT.Interop.InitializeWithWindow.Initialize(picker, WinRT.Interop.WindowNative.GetWindowHandle(App.CurrentWindow!));

    private async void ImportFiles(object sender, RoutedEventArgs e) => await Perform(async () => {
        var picker = new FileOpenPicker(); picker.FileTypeFilter.Add(".pulse"); picker.FileTypeFilter.Add(".zip"); InitializePicker(picker);
        var files = await picker.PickMultipleFilesAsync();
        if (files.Count > 0) await ImportPathsAsync(files.Select(f => f.Path));
    });

    private async void ImportFolder(object sender, RoutedEventArgs e) => await Perform(async () => {
        var picker = new FolderPicker(); picker.FileTypeFilter.Add("*"); InitializePicker(picker);
        var folder = await picker.PickSingleFolderAsync();
        if (folder != null) await ImportPathsAsync(new[] { folder.Path });
    });

    public async Task ImportPathsAsync(IEnumerable<string> paths)
    {
        var data = await _model!.WaveformOperationAsync("waveform_import", new() { ["kind"] = Kind, ["paths"] = new JsonArray(paths.Select(p => (JsonNode?)JsonValue.Create(p)).ToArray()) });
        ApplyCatalog(data["catalog"]!.AsObject());
        var report = data["report"]!;
        Notice.Message = $"已导入 {report["imported"]}，重复跳过 {report["duplicates"]}，无效 {report["failed"]}。拖到右侧后即可选用。";
        Notice.Severity = report["failed"]!.GetValue<int>() > 0 ? InfoBarSeverity.Warning : InfoBarSeverity.Informational;
        ToolTipService.SetToolTip(Notice, string.Join(Environment.NewLine, report["errors"]!.AsArray().Select(n => n!.ToString())));
        Notice.IsOpen = true;
    }

    private async void SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_updating || _model == null || !_active || e.AddedItems.LastOrDefault() is not WaveformItem item) return;
        int version = ++_previewVersion;
        ClearPreview(); PreviewTitle.Text = item.Name;
        try {
            var data = await _model.WaveformOperationAsync("waveform_preview", new() { ["kind"] = Kind, ["id"] = item.Id });
            if (version != _previewVersion || !_active) return;
            _preview = Convert.FromHexString(data["sample_hex"]!.GetValue<string>());
            _previewFrequency = Convert.FromHexString(data["frequency_hex"]?.GetValue<string>() ?? "");
            if (_previewFrequency.Length != 0 && _previewFrequency.Length != _preview.Length) throw new InvalidOperationException("预览频率样本长度无效。");
            _previewPhase = BuildPhase(_previewFrequency);
            PreviewPlayback.IsEnabled = _preview.Length > 0;
            SetPreviewPlayback(true);
        }
        catch (Exception exc) { if (_active && version == _previewVersion) { Notice.Message = exc.Message; Notice.IsOpen = true; } }
    }

    private void PreviewSizeChanged(object sender, SizeChangedEventArgs e) => DrawPreview();
    private void ClearPreview()
    {
        StopPreviewTimer(); _previewClock.Reset();
        _preview = Array.Empty<byte>(); _histogramBars.Clear(); PreviewCanvas.Children.Clear();
        _previewFrequency = Array.Empty<byte>(); _previewPhase = Array.Empty<double>();
        PreviewPlayback.IsEnabled = false; PreviewPlayback.IsChecked = true; PreviewPlaybackIcon.Glyph = "\uE769";
        Microsoft.UI.Xaml.Automation.AutomationProperties.SetName(PreviewPlayback, "暂停预览");
        PreviewTitle.Text = "";
    }
    private void StopPreviewTimer() { _previewTimer.Stop(); _previewClock.Stop(); }
    private void TogglePreviewPlayback(object sender, RoutedEventArgs e) => SetPreviewPlayback(PreviewPlayback.IsChecked == true);
    internal void SetPreviewPlayback(bool play)
    {
        PreviewPlayback.IsChecked = play; PreviewPlaybackIcon.Glyph = play ? "\uE769" : "\uE768";
        Microsoft.UI.Xaml.Automation.AutomationProperties.SetName(PreviewPlayback, play ? "暂停预览" : "播放预览");
        if (play && _active && IsLoaded && _preview.Length > 0) { _previewClock.Start(); _previewTimer.Start(); }
        else StopPreviewTimer();
        DrawPreview();
    }
    private void PreviewTick(object? sender, object e)
    {
        if (!_active || !IsLoaded || _preview.Length == 0 || PreviewPlayback.IsChecked != true) { StopPreviewTimer(); return; }
        // Do not continuously repaint a hidden/tray window. Resume the visual
        // clock on restore; this timer never submits a hardware command.
        if (App.CurrentWindow?.AppWindow.IsVisible != true) {
            _previewClock.Stop(); _previewTimer.Interval = TimeSpan.FromSeconds(1); return;
        }
        _previewTimer.Interval = TimeSpan.FromMilliseconds(33); _previewClock.Start(); DrawPreview();
    }
    private void DrawPreview()
    {
        double width = Math.Max(0, PreviewCanvas.ActualWidth - 2), height = Math.Max(0, PreviewCanvas.ActualHeight - 4);
        if (_preview.Length == 0 || width <= 0 || height <= 0) return;
        double elapsedMs = _previewClock.Elapsed.TotalMilliseconds;
        var geometry = PreviewGeometry(_preview, _previewPhase, elapsedMs, width, height);
        while (_histogramBars.Count < geometry.Count) {
            var bar = new Rectangle { Style = (Style)Resources["HistogramBarStyle"], IsHitTestVisible = false };
            _histogramBars.Add(bar); PreviewCanvas.Children.Add(bar);
        }
        for (int i = 0; i < _histogramBars.Count; i++) {
            var bar = _histogramBars[i];
            bar.Visibility = i < geometry.Count ? Visibility.Visible : Visibility.Collapsed;
            if (i >= geometry.Count) continue;
            var point = geometry[i];
            bar.Width = point.Width; bar.Height = point.Height; bar.Opacity = .8;
            Canvas.SetLeft(bar, 1 + point.X); Canvas.SetTop(bar, 2 + height - bar.Height);
        }
        PreviewFrameCount++;
    }

    internal readonly record struct PreviewBar(double X, double Width, double Height);

    internal static double[] BuildPhase(byte[] frequencies)
    {
        if (frequencies.Length == 0) return Array.Empty<double>();
        var phase = new double[frequencies.Length + 1];
        for (int i = 0; i < frequencies.Length; i++) phase[i + 1] = phase[i] + .025 * FrequencyHz(frequencies[i]);
        return phase;
    }

    // Integrate frequency through each exact 25 ms sample, preserving phase at
    // changes and loop boundaries. One bar per cycle: height is amplitude and
    // spacing is actual period. A fixed 2 s window bounds rendering to <= 201
    // pulses at 100 Hz. Opossum has no electrical frequency: show its envelope.
    internal static List<PreviewBar> PreviewGeometry(byte[] amplitudes, double[] phase, double elapsedMs, double width, double height)
    {
        var bars = new List<PreviewBar>();
        if (amplitudes.Length == 0 || width <= 0 || height <= 0) return bars;
        const double window = 2000;
        double left = elapsedMs - window;
        long start = (long)Math.Floor(left / 25), end = (long)Math.Ceiling(elapsedMs / 25);
        for (long cursor = start; cursor < end; cursor++) {
            int index = (int)((cursor % amplitudes.Length + amplitudes.Length) % amplitudes.Length);
            double from = Math.Max(left, cursor * 25), to = Math.Min(elapsedMs, (cursor + 1) * 25);
            double amplitude = height * Math.Clamp((int)amplitudes[index], 0, 100) / 100;
            if (phase.Length == 0) {
                double x = (from - left) / window * width;
                bars.Add(new(x, Math.Max(0, (to - from) / window * width - 1), amplitude));
                continue;
            }
            double cyclesPerSample = phase[index + 1] - phase[index];
            double absolutePhase = Math.Floor((double)cursor / amplitudes.Length) * phase[^1] + phase[index];
            double phaseFrom = absolutePhase + (from - cursor * 25) / 25 * cyclesPerSample;
            double phaseTo = absolutePhase + (to - cursor * 25) / 25 * cyclesPerSample;
            for (double cycle = Math.Ceiling(phaseFrom - 1e-9); cycle < phaseTo - 1e-9; cycle++) {
                double time = cursor * 25 + (cycle - absolutePhase) / cyclesPerSample * 25;
                double x = Math.Clamp((time - left) / window * width, 0, width);
                double barWidth = Math.Min(3, 25 / cyclesPerSample / window * width * .3);
                bars.Add(new(x, Math.Min(barWidth, width - x), amplitude));
            }
        }
        return bars;
    }

    // Official Coyote V3 period compression; display real Hz, not wire bytes.
    internal static double FrequencyHz(byte encoded)
    {
        double period = encoded <= 100 ? encoded : encoded <= 200 ? 100 + (encoded - 100) * 5 : 600 + (encoded - 200) * 10;
        return 1000 / Math.Clamp(period, 10, 1000);
    }

    private void WaveDragStarting(object sender, DragItemsStartingEventArgs e)
    {
        var ids = e.Items.OfType<WaveformItem>().Select(p => (JsonNode?)JsonValue.Create(p.Id)).ToArray();
        e.Data.SetText(DragPrefix + new JsonObject { ["kind"] = Kind, ["source"] = ReferenceEquals(sender, EnabledList) ? "enabled" : "library", ["ids"] = new JsonArray(ids) }.ToJsonString());
        e.Data.RequestedOperation = DataPackageOperation.Move;
    }
    private void DragOverList(object sender, DragEventArgs e)
    {
        if (!_working && e.DataView.Contains(StandardDataFormats.Text)) e.AcceptedOperation = DataPackageOperation.Move;
    }
    private async void DropOnList(object sender, DragEventArgs e)
    {
        if (_working || !e.DataView.Contains(StandardDataFormats.Text)) return;
        var deferral = e.GetDeferral();
        try {
            string text = await e.DataView.GetTextAsync();
            if (!text.StartsWith(DragPrefix, StringComparison.Ordinal)) return;
            var data = JsonNode.Parse(text[DragPrefix.Length..])!;
            if (data["kind"]!.GetValue<string>() != Kind) { e.AcceptedOperation = DataPackageOperation.None; e.Handled = true; return; }
            bool targetEnabled = ReferenceEquals(sender, EnabledList);
            bool sourceEnabled = data["source"]!.GetValue<string>() == "enabled";
            // Same-list reordering is handled by WinUI's native ListView.
            if (targetEnabled == sourceEnabled) return;
            e.Handled = true;
            var ids = data["ids"]!.AsArray().Select(n => n!.GetValue<string>()).ToHashSet();
            await MoveSelectionAsync(ids, targetEnabled);
        }
        catch (Exception exc) { e.Handled = true; Notice.Message = exc.Message; Notice.IsOpen = true; }
        finally { deferral.Complete(); }
    }
    private async void AddSelected(object sender, RoutedEventArgs e) => await MoveSelectionAsync(LibraryList.SelectedItems.OfType<WaveformItem>().Select(p => p.Id).ToHashSet(), true);
    private async void RemoveSelected(object sender, RoutedEventArgs e) => await MoveSelectionAsync(EnabledList.SelectedItems.OfType<WaveformItem>().Select(p => p.Id).ToHashSet(), false);

    internal Task MoveSelectionAsync(HashSet<string> ids, bool enable) => Perform(async () => {
        var source = enable ? _library : _enabled;
        var target = enable ? _enabled : _library;
        _updating = true;
        foreach (var item in source.Where(p => ids.Contains(p.Id)).ToArray()) { source.Remove(item); target.Add(item); }
        _updating = false;
        await PersistAsync();
    });

    private async void ReorderCompleted(ListViewBase sender, DragItemsCompletedEventArgs args)
    {
        if (!_working && !_updating && _active && args.DropResult == DataPackageOperation.Move) await Perform(PersistAsync);
    }
    private async Task PersistAsync()
    {
        var ids = _enabled.Select(p => p.Id).ToArray();
        if (_catalog[Kind]!["enabled"]!.AsArray().Select(n => n!.GetValue<string>()).SequenceEqual(ids)) return;
        try {
            var response = await _model!.WaveformOperationAsync("waveform_select", new() { ["kind"] = Kind, ["enabled"] = new JsonArray(ids.Select(p => (JsonNode?)JsonValue.Create(p)).ToArray()) });
            _catalog = (JsonObject)response["catalog"]!.DeepClone(); Notice.IsOpen = false;
        }
        catch { Populate(); throw; }
    }
}
