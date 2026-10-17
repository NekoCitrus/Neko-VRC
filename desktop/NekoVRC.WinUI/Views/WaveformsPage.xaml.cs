using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Linq;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Navigation;
using Windows.ApplicationModel.DataTransfer;
using Windows.Foundation;
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
    private double[] _preview = Array.Empty<double>();
    private int _previewVersion;
    private bool _updating, _working, _active;
    public Task LoadTask { get; private set; } = Task.CompletedTask;
    private string Kind => (KindBox.SelectedItem as ComboBoxItem)?.Tag?.ToString() ?? "coyote";

    public WaveformsPage()
    {
        InitializeComponent();
        LibraryList.ItemsSource = _library; EnabledList.ItemsSource = _enabled;
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
        _active = false; _previewVersion++;
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
        FormatHint.Text = Kind == "coyote" ? "支持郊狼 .pulse、ZIP 或文件夹。拖到右侧加入选择框，拖回左侧移出。" : "负鼠库独立保存。仅导入标记为 opossum 的帧文件；郊狼 .pulse 不通用。";
        _preview = Array.Empty<double>(); DrawPreview(); PreviewTitle.Text = "选择波形以预览";
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
        try {
            var data = await _model.WaveformOperationAsync("waveform_preview", new() { ["kind"] = Kind, ["id"] = item.Id });
            if (version != _previewVersion || !_active) return;
            _preview = data["amplitudes"]!.AsArray().Select(n => n!.GetValue<double>()).ToArray();
            PreviewTitle.Text = $"{item.Name} · {data["duration"]!.GetValue<double>():0.###} 秒"; DrawPreview();
        }
        catch (Exception exc) { if (_active && version == _previewVersion) { Notice.Message = exc.Message; Notice.IsOpen = true; } }
    }

    private void PreviewSizeChanged(object sender, SizeChangedEventArgs e) => DrawPreview();
    private void DrawPreview()
    {
        var points = new PointCollection();
        double width = Math.Max(0, PreviewArea.ActualWidth - 2), height = Math.Max(0, PreviewArea.ActualHeight - 4);
        for (int i = 0; i < _preview.Length; i++) {
            double x = 1 + i * width / Math.Max(1, _preview.Length - 1);
            if (i > 0) points.Add(new Point(x, 2 + (1 - _preview[i - 1] / 100) * height));
            points.Add(new Point(x, 2 + (1 - _preview[i] / 100) * height));
        }
        PreviewLine.Points = points;
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
