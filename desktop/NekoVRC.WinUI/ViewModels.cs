using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Linq;
using System.Runtime.CompilerServices;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using System.Windows.Input;

using BackendClient = NekoVRC.Desktop.BackendClient;

namespace NekoVRC.WinUI;

public class Observable : INotifyPropertyChanged
{
    public event PropertyChangedEventHandler? PropertyChanged;
    protected void Changed([CallerMemberName] string? property = null) => PropertyChanged?.Invoke(this, new(property));
    protected void Set<T>(ref T field, T value, [CallerMemberName] string? property = null) { field = value; Changed(property); }
}

public sealed class Command : ICommand
{
    private static readonly List<WeakReference<Command>> Instances = new();
    private readonly Action<object?> _execute;
    private readonly Func<bool>? _canExecute;
    public Command(Action<object?> execute, Func<bool>? canExecute = null)
    {
        _execute = execute; _canExecute = canExecute;
        Instances.Add(new(this));
    }
    public bool CanExecute(object? parameter) => _canExecute?.Invoke() ?? true;
    public void Execute(object? parameter) => _execute(parameter);
    public event EventHandler? CanExecuteChanged;
    public static void Requery()
    {
        Instances.RemoveAll(reference => !reference.TryGetTarget(out _));
        foreach (var reference in Instances.ToArray())
            if (reference.TryGetTarget(out var command)) command.CanExecuteChanged?.Invoke(command, EventArgs.Empty);
    }
}

public sealed record Zone(string Id, string Name) { public override string ToString() => Name; }
public sealed record ConnectionMode(string Id, string Name);
public sealed record Credit(string Name, string Url);
public sealed record BluetoothDevice(string Address, string Name, string Model, string Kind)
{
    public string Label => $"{Name}  ·  {Address}";
    public override string ToString() => Label;
}

public sealed class ChannelViewModel : Observable
{
    public string Name { get; }
    public string Key { get; }
    private readonly JsonObject _basic;
    private readonly JsonObject _runtime;
    private bool _socketEnabled;
    public bool SocketEnabled { get => _socketEnabled; set => Set(ref _socketEnabled, value); }
    private bool _plugEnabled;
    public bool PlugEnabled { get => _plugEnabled; set => Set(ref _plugEnabled, value); }
    private bool _pcsEnabled;
    public bool PcsEnabled { get => _pcsEnabled; set => Set(ref _pcsEnabled, value); }
    private bool _lmsEnabled;
    public bool LmsEnabled { get => _lmsEnabled; set => Set(ref _lmsEnabled, value); }
    private bool _extraEnabled;
    public bool ExtraEnabled { get => _extraEnabled; set => Set(ref _extraEnabled, value); }
    private string _socketZone = "*", _plugZone = "*";
    public string SocketZone { get => _socketZone; set { if (!string.IsNullOrEmpty(value)) Set(ref _socketZone, value); } }
    public string PlugZone { get => _plugZone; set { if (!string.IsNullOrEmpty(value)) Set(ref _plugZone, value); } }
    private string _limit = "";
    private double _limitValue = 100;
    public string Limit { get => _limit; set {
        if (_limit == value) return;
        Set(ref _limit, value);
        // Allow temporarily empty/invalid text while editing; Save validates it.
        if (int.TryParse(value, out int limit) && limit >= 0 && limit <= 200) {
            _limitValue = limit; Changed(nameof(LimitValue));
        }
    } }
    public double LimitValue { get => _limitValue; set {
        if (!double.IsFinite(value)) return;
        Limit = Math.Clamp((int)Math.Round(value), 0, 200).ToString();
    } }
    private string _waveform = "";
    public string Waveform { get => _waveform; set { if (!string.IsNullOrEmpty(value)) Set(ref _waveform, value); } }
    private string _extraPaths = "";
    public string ExtraPaths { get => _extraPaths; set => Set(ref _extraPaths, value); }
    public ObservableCollection<Zone> SocketZones { get; } = new();
    public ObservableCollection<Zone> PlugZones { get; } = new();
    public IReadOnlyList<string> Waveforms { get; }
    public IReadOnlyList<Zone> WaveformChoices { get; }
    private string _debug = "无输出";
    public string Debug { get => _debug; set => Set(ref _debug, value); }
    private string _parameterHint = "等待读取模型 OSC 参数";
    public string ParameterHint { get => _parameterHint; set => Set(ref _parameterHint, value); }

    public ChannelViewModel(string name, string kind, JsonObject basic, JsonObject runtime, JsonArray waveforms)
    {
        Name = name + " 通道"; Key = "channel_" + name.ToLowerInvariant();
        _basic = (JsonObject)basic.DeepClone(); _runtime = (JsonObject)runtime.DeepClone();
        SocketEnabled = basic["socket_enabled"]?.GetValue<bool>() ?? true;
        PlugEnabled = basic["plug_enabled"]?.GetValue<bool>() ?? true;
        PcsEnabled = basic["pcs_enabled"]?.GetValue<bool>() ?? kind == "coyote";
        LmsEnabled = basic["lms_enabled"]?.GetValue<bool>() ?? kind == "opossum";
        ExtraEnabled = basic["extra_parameters"]!["enabled"]!.GetValue<bool>();
        SocketZone = basic["socket_zone"]!.GetValue<string>(); PlugZone = basic["plug_zone"]!.GetValue<string>();
        Limit = basic["strength_limit"]!.ToString(); Waveform = runtime["depth"]!["waveform"]!.GetValue<string>();
        ExtraPaths = string.Join(Environment.NewLine, basic["extra_parameters"]!["paths"]!.AsArray().Select(p => p!.GetValue<string>()));
        Waveforms = waveforms.Select(p => p!.GetValue<string>()).ToArray();
        WaveformChoices = Waveforms.Select(name => new Zone(name, name)).ToArray();
        UpdateZones(null, null);
    }

    public void UpdateZones(JsonArray? sockets, JsonArray? plugs)
    {
        static void Populate(ObservableCollection<Zone> items, string selected, JsonArray? zones) {
            var next = new List<Zone> { new("*", "任何正在触发的部位") };
            if (zones != null) foreach (JsonNode? zone in zones) {
                string? id = zone?["zone_id"]?.GetValue<string>();
                if (!string.IsNullOrEmpty(id) && next.All(z => z.Id != id))
                    next.Add(new(id, zone?["label"]?.GetValue<string>() ?? id));
            }
            if (selected != "*" && next.All(z => z.Id != selected)) next.Add(new(selected, selected + "（未检测到）"));
            if (items.SequenceEqual(next)) return;
            items.Clear(); foreach (var zone in next) items.Add(zone);
        }
        // Preserve configured IDs while rebuilding inventories after avatar changes.
        string socket = SocketZone, plug = PlugZone;
        Populate(SocketZones, socket, sockets); Populate(PlugZones, plug, plugs);
        SocketZone = socket; PlugZone = plug; Changed(nameof(SocketZone)); Changed(nameof(PlugZone));
    }

    public (JsonObject basic, JsonObject runtime) Export()
    {
        if (!int.TryParse(Limit, out int limit) || limit < 0 || limit > 200) throw new ArgumentException(Name + "上限应为 0~200。");
        var basic = (JsonObject)_basic.DeepClone(); var runtime = (JsonObject)_runtime.DeepClone();
        basic["socket_enabled"] = SocketEnabled; basic["plug_enabled"] = PlugEnabled;
        basic["pcs_enabled"] = PcsEnabled; basic["lms_enabled"] = LmsEnabled;
        basic["socket_zone"] = SocketZone; basic["plug_zone"] = PlugZone; basic["strength_limit"] = limit;
        basic["extra_parameters"]!["enabled"] = ExtraEnabled;
        basic["extra_parameters"]!["paths"] = new JsonArray(ExtraPaths.Split(new[] { '\r', '\n' }, StringSplitOptions.RemoveEmptyEntries).Select(p => (JsonNode?)JsonValue.Create(p.Trim())).ToArray());
        runtime["depth"]!["waveform"] = Waveform;
        return (basic, runtime);
    }
}

public sealed class DeviceViewModel : Observable
{
    public string Kind { get; }
    public string Name { get; }
    public IReadOnlyList<ConnectionMode> ConnectionModes { get; } = new[] { new ConnectionMode("bluetooth", "蓝牙直连"), new ConnectionMode("app", "App") };
    private string _mode = "bluetooth", _address = "", _model = "";
    public string Mode { get => _mode; set { if (ConnectionModes.Any(mode => mode.Id == value)) Set(ref _mode, value); } }
    public string Address { get => _address; set => Set(ref _address, value); }
    public string Model { get => _model; set => Set(ref _model, value); }
    public ObservableCollection<BluetoothDevice> Devices { get; } = new();
    public ObservableCollection<ChannelViewModel> Channels { get; } = new();
    private BluetoothDevice? _selected;
    public BluetoothDevice? SelectedDevice { get => _selected; set {
        // ComboBox emits a transient null while a page binds/unbinds. Keep a
        // still-valid choice; a real scan clearing Devices can still clear it.
        if (value == null && _selected != null && Devices.Contains(_selected)) return;
        _selected = value;
        if (value != null) { Address = value.Address; Model = value.Model; Changed(nameof(Address)); }
        Changed();
    } }
    private string _status = "未连接";
    public string Status { get => _status; set => Set(ref _status, value); }
    private string _scanStatus = "";
    public string ScanStatus { get => _scanStatus; private set => Set(ref _scanStatus, value); }
    public ICommand ScanCommand { get; }
    public ICommand ConnectCommand { get; }
    public ICommand DisconnectCommand { get; }
    public ICommand QrCommand { get; }

    public DeviceViewModel(string kind, MainViewModel owner, JsonObject config)
    {
        Kind = kind; Name = kind == "coyote" ? "郊狼" : "负鼠";
        QrCommand = owner.QrCommand;
        Mode = config["mode"]?.GetValue<string>() ?? "bluetooth";
        Address = config["address"]!.GetValue<string>(); Model = config["model"]!.GetValue<string>();
        ScanCommand = new Command(_ => owner.Run(() => owner.ScanAsync(this)), () => !owner.Busy);
        ConnectCommand = new Command(_ => owner.Run(async () => {
            if (Mode != "bluetooth") throw new InvalidOperationException("App 模式请扫码连接。");
            await owner.SaveAsync(); await owner.DeviceActionAsync("connect", Kind);
        }), () => !owner.Busy);
        DisconnectCommand = new Command(_ => owner.Run(() => owner.DeviceActionAsync("disconnect", Kind)), () => !owner.Busy);
    }

    public async Task ScanAsync(Func<Task<JsonArray>> discover)
    {
        ScanStatus = "扫描蓝牙设备…";
        try {
            var results = await discover();
            Devices.Clear();
            foreach (JsonNode? item in results) {
                if (item!["device_kind"]!.GetValue<string>() == Kind)
                    Devices.Add(new(item["address"]!.GetValue<string>(), item["name"]!.GetValue<string>(), item["model"]!.GetValue<string>(), Kind));
            }
            SelectedDevice = Devices.FirstOrDefault(d => d.Address == Address) ?? Devices.FirstOrDefault();
            ScanStatus = Devices.Count == 0 ? "未找到设备，请释放手机蓝牙连接并开启广播。" : $"扫描完成，找到 {Devices.Count} 台设备";
        }
        catch (Exception exc) { ScanStatus = "扫描失败：" + exc.Message; }
    }
}

public sealed class MainViewModel : Observable
{
    public IReadOnlyList<Credit> Credits { get; } = new[] {
        new Credit("Neko-VRC", "https://github.com/NekoCitrus/Neko-VRC"),
        new Credit("Shocking-VRChat", "https://github.com/VRChatNext/Shocking-VRChat"),
        new Credit("DG-LAB", "https://github.com/dungeonlab-open"),
        new Credit("DG-LAB 协议", "https://github.com/dungeonlab-open/dglab-bluetooth-protocol"),
        new Credit("DG-LAB 波形 / SDK", "https://github.com/dungeonlab-open/dglab-kit"),
        new Credit("DG-LAB-VRCOSC", "https://github.com/ccvrc/DG-LAB-VRCOSC"),
        new Credit("OscGoesBrrr", "https://github.com/OscToys/OscGoesBrrr"),
        new Credit("osc.toys 源码", "https://github.com/OscToys/osc.toys"),
        new Credit("osc.toys", "https://osc.toys"),
        new Credit("WinUI 3", "https://github.com/microsoft/microsoft-ui-xaml"),
        new Credit("Windows Community Toolkit", "https://github.com/CommunityToolkit/Windows"),
        new Credit("WSL 设置界面参考", "https://github.com/microsoft/WSL/tree/master/src/windows/wslsettings"),
        new Credit("Bleak", "https://github.com/hbldh/bleak"),
    };
    private readonly BackendClient _client;
    private JsonObject _settings = new(), _basic = new();
    private string _page = "general", _status = "连接后台…", _avatar = "尚未读取 Avatar", _logs = "";
    private bool _busy, _ready;
    public bool Busy { get => _busy; private set { Set(ref _busy, value); Command.Requery(); } }
    public bool Ready { get => _ready; private set => Set(ref _ready, value); }
    public string Status { get => _status; set { Set(ref _status, value); Changed(nameof(StatusLine)); } }
    public string Avatar { get => _avatar; set { Set(ref _avatar, value); Changed(nameof(StatusLine)); } }
    private string _parameterHint = "等待读取模型 OSC 参数";
    public string ParameterHint { get => _parameterHint; private set { Set(ref _parameterHint, value); Changed(nameof(StatusLine)); } }
    public string StatusLine => string.Join(" · ", new[] { Status, ParameterHint, string.IsNullOrWhiteSpace(Avatar) || Avatar == "尚未读取 Avatar" ? "" : Avatar }.Where(part => !string.IsNullOrWhiteSpace(part)));
    public string Logs { get => _logs; set => Set(ref _logs, value); }
    private bool _background, _steamVR, _chatbox;
    public bool Background { get => _background; set => Set(ref _background, value); }
    public bool SteamVR { get => _steamVR; set => Set(ref _steamVR, value); }
    public bool Chatbox { get => _chatbox; set => Set(ref _chatbox, value); }
    public IReadOnlyList<Zone> ThemeChoices { get; } = new[] { new Zone("system", "跟随系统"), new Zone("light", "浅色模式"), new Zone("dark", "深色模式") };
    private string _theme = "system";
    public Task ThemeSaveTask { get; private set; } = Task.CompletedTask;
    public string Theme { get => _theme; set {
        if (_theme == value || !ThemeChoices.Any(choice => choice.Id == value)) return;
        Set(ref _theme, value);
        if (Ready) {
            _settings["general"]!["theme"] = value;
            ThemeSaveTask = PersistThemeAsync(ThemeSaveTask, value);
        }
    } }
    private async Task PersistThemeAsync(Task previous, string theme)
    {
        await previous;
        try { await _client.CallAsync("set_theme", new() { ["theme"] = theme }); }
        catch (Exception exc) { OperationError?.Invoke("主题保存失败：" + exc.Message); }
    }
    public ObservableCollection<DeviceViewModel> Devices { get; } = new();
    public DeviceViewModel? CurrentDevice => Devices.FirstOrDefault(d => d.Kind == _page);
    public bool IsGeneral => _page == "general";
    public bool IsCoyote => _page == "coyote";
    public bool IsOpossum => _page == "opossum";
    public bool IsDevice => _page is "coyote" or "opossum";
    public bool IsDebug => _page == "debug";
    public bool IsAbout => _page == "about";
    public string ConfigPath { get; private set; } = "";
    public ICommand PageCommand { get; }
    public ICommand SaveCommand { get; }
    public ICommand StopCommand { get; }
    public ICommand ResumeCommand { get; }
    public event Action<JsonArray>? QrReady;
    public event Action<string>? OperationError;
    public ICommand QrCommand { get; }

    public MainViewModel(BackendClient client)
    {
        _client = client;
        PageCommand = new Command(p => SelectPage(p?.ToString() ?? "general"));
        SaveCommand = new Command(_ => Run(SaveAsync), () => Ready && !Busy);
        StopCommand = new Command(_ => { _client.EmergencyStop(); Status = "正在暂停…"; }, () => Ready);
        ResumeCommand = new Command(_ => Run(() => DeviceActionAsync("resume")), () => Ready && !Busy);
        QrCommand = new Command(_ => Run(async () => {
            var response = await _client.CallAsync("qr"); QrReady?.Invoke(response["data"]!["matrix"]!.AsArray());
        }), () => Ready && !Busy);
    }

    public void SelectPage(string page)
    {
        _page = page;
        foreach (string property in new[] { nameof(IsGeneral), nameof(IsCoyote), nameof(IsOpossum), nameof(IsDevice), nameof(IsDebug), nameof(IsAbout), nameof(CurrentDevice) }) Changed(property);
    }

    public async Task InitializeAsync()
    {
        var response = await _client.CallAsync("init");
        var data = response["data"]!.AsObject();
        _settings = (JsonObject)data["settings"]!.DeepClone(); _basic = (JsonObject)data["basic"]!.DeepClone();
        Background = _settings["general"]!["run_in_background"]!.GetValue<bool>();
        SteamVR = _settings["general"]!["steamvr_auto_start"]!.GetValue<bool>();
        Chatbox = _settings["chatbox"]!["enable"]!.GetValue<bool>();
        Theme = _settings["general"]!["theme"]?.GetValue<string>() ?? "system";
        ConfigPath = data["config_path"]!.GetValue<string>(); Changed(nameof(ConfigPath));
        foreach (string kind in new[] { "coyote", "opossum" }) {
            var device = new DeviceViewModel(kind, this, _settings["connections"]![kind]!.AsObject());
            foreach (string channel in new[] { "A", "B" }) {
                string key = "channel_" + channel.ToLowerInvariant();
                device.Channels.Add(new(channel, kind, _basic["dglab3"]![kind]![key]!.AsObject(), _settings["dglab3"]![kind]![key]!.AsObject(), data["waveforms"]![kind]!.AsArray()));
            }
            Devices.Add(device);
        }
        Ready = true; Status = "后台已连接";
        foreach (string p in new[] { nameof(Background), nameof(SteamVR), nameof(Chatbox), nameof(CurrentDevice) }) Changed(p);
        Command.Requery();
    }

    public async void Run(Func<Task> action)
    {
        if (Busy) return;
        Busy = true;
        try { await action(); OperationError?.Invoke(""); }
        catch (Exception exc) { Status = exc.Message; OperationError?.Invoke(exc.Message); }
        finally { Busy = false; }
    }

    public async Task SaveAsync()
    {
        await ThemeSaveTask;
        var settings = (JsonObject)_settings.DeepClone(); var basic = (JsonObject)_basic.DeepClone();
        settings["general"]!["run_in_background"] = Background;
        settings["general"]!["steamvr_auto_start"] = SteamVR;
        settings["general"]!["theme"] = Theme;
        settings["chatbox"]!["enable"] = Chatbox;
        foreach (var device in Devices) {
            settings["connections"]![device.Kind]!["mode"] = device.Mode;
            settings["connections"]![device.Kind]!["address"] = device.Address;
            settings["connections"]![device.Kind]!["model"] = device.Model;
            foreach (var channel in device.Channels) {
                var exported = channel.Export();
                basic["dglab3"]![device.Kind]![channel.Key] = exported.basic;
                settings["dglab3"]![device.Kind]![channel.Key] = exported.runtime;
            }
        }
        await _client.CallAsync("save", new() { ["settings"] = settings, ["basic"] = basic });
        _settings = settings; _settings["general"]!["theme"] = Theme; _basic = basic; Status = "已保存并应用";
    }

    public async Task DeviceActionAsync(string action, string? kind = null)
    {
        var response = await _client.CallAsync(action, kind == null ? null : new() { ["kind"] = kind });
        UpdateState(response["data"]!.AsObject());
    }

    public Task ScanAsync(DeviceViewModel device) => device.ScanAsync(async () =>
        (await _client.CallAsync("scan"))["data"]!.AsArray());

    public async Task PollAsync()
    {
        if (!Ready || Busy) return;
        var response = await _client.CallAsync("snapshot"); UpdateState(response["data"]!.AsObject());
    }

    public void UpdateState(JsonObject data)
    {
        var avatar = data["avatar"]?.AsObject();
        Avatar = avatar?["avatar_name"]?.GetValue<string>() ?? "尚未读取 Avatar";
        var available = avatar?["parameter_availability"];
        bool known = available?["known"]?.GetValue<bool>() ?? (avatar?["sockets"] != null && avatar?["plugs"] != null);
        bool sockets = available?["socket"]?.GetValue<bool>() ?? avatar?["sockets"]?.AsArray().Count > 0;
        bool plugs = available?["plug"]?.GetValue<bool>() ?? avatar?["plugs"]?.AsArray().Count > 0;
        var fallbacks = new List<string>();
        if (!sockets && available?["pcs"]?.GetValue<bool>() == true) fallbacks.Add("PCS");
        if (!plugs && available?["lms"]?.GetValue<bool>() == true) fallbacks.Add("LMS");
        ParameterHint = !known ? "等待读取模型 OSC 参数" : fallbacks.Count > 0 ? "推荐使用 " + string.Join("/", fallbacks) + " 回退" : !sockets && !plugs ? "无任何可用插座/插头参数" : "";
        var runtime = data["runtime"]?.AsObject();
        bool paused = runtime?["output_paused"]?.GetValue<bool>() ?? false;
        Status = paused ? "已暂停 · 点击恢复后重新接收触发" : data["service"]?.GetValue<string>() == "running" ? "服务运行中" : "服务已停止";
        Logs = string.Join(Environment.NewLine, data["logs"]?.AsArray().Select(x => x!.GetValue<string>()) ?? Array.Empty<string>());
        foreach (var device in Devices) {
            var hosts = runtime?["devices"]?.AsArray().Where(x => x?["device_kind"]?.GetValue<string>() == device.Kind).ToArray() ?? Array.Empty<JsonNode?>();
            device.Status = hosts.Length == 0 ? "未连接" : string.Join(" / ", hosts.Select(x =>
                (x?["transport"]?.GetValue<string>() == "bluetooth" ? "蓝牙" : "App") + " · " + x?["device_name"]?.GetValue<string>() +
                (x?["battery"] != null ? " · 电量 " + x["battery"] + "%" : "")));
            var ble = runtime?["bluetooth"]?[device.Kind];
            if (hosts.Length == 0 && ble?["error"] != null) device.Status = ble["error"]!.GetValue<string>();
            foreach (var channel in device.Channels) {
                channel.UpdateZones(avatar?["sockets"]?.AsArray(), avatar?["plugs"]?.AsArray());
                var info = runtime?["device_channels"]?[device.Kind]?[channel.Name[..1]];
                channel.ParameterHint = (info?["parameter_hint"]?.GetValue<string>() ?? "等待读取模型 OSC 参数").Replace("Socket", "插座深度").Replace("Plug", "插头深度");
                double percent = info?["strength_percentage"]?.GetValue<double>() ?? 0;
                string samples = info?["sent_strengths"]?.ToJsonString() ?? "[0,0,0,0]";
                string source = info?["trigger_type"]?.GetValue<string>() ?? "—";
                source = source switch { "sps_socket" => "插座深度", "sps_plug" => "插头深度", _ => source };
                channel.Debug = $"触发：{source}　比例：{percent:P0}\n已发送波形幅值：{samples}　有效上限：{info?["upper_strength"] ?? JsonValue.Create(0)}\n设备反馈强度：{info?["actual_strength"] ?? JsonValue.Create(0)}";
            }
        }
    }

    public void PrepareSmokePorts(int ws, int web, int query, int osc)
    {
        _settings["ws"]!["listen_port"] = ws; _settings["web_server"]!["listen_port"] = web;
        _settings["oscquery"]!["listen_port"] = query; _settings["osc"]!["listen_port"] = osc;
        Chatbox = false;
    }
}
