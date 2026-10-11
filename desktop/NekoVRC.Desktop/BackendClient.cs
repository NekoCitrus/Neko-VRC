using System;
using System.Diagnostics;
using System.IO;
using System.IO.Pipes;
using System.Text;
using System.Text.Json.Nodes;
using System.Threading;
using System.Threading.Tasks;

namespace NekoVRC.Desktop;

public sealed class BackendClient : IAsyncDisposable
{
    private NamedPipeServerStream? _pipe;
    private StreamReader? _reader;
    private StreamWriter? _writer;
    private Process? _process;
    private EventWaitHandle? _stopSignal;
    private readonly SemaphoreSlim _gate = new(1);
    private int _counter;
    public string Diagnostics { get; private set; } = "";

    public async Task OpenAsync(string? configDir = null)
    {
        string name = "NekoVRC-" + Guid.NewGuid().ToString("N");
        _stopSignal = new EventWaitHandle(false, EventResetMode.AutoReset, @"Local\" + name + "-Stop");
        _pipe = new NamedPipeServerStream(name, PipeDirection.InOut, 1, PipeTransmissionMode.Byte,
            PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly);
        string packaged = Path.Combine(AppContext.BaseDirectory, "backend", "Neko-VRC.Backend.exe");
        var start = new ProcessStartInfo { UseShellExecute = false, CreateNoWindow = true, RedirectStandardError = true };
        if (File.Exists(packaged)) start.FileName = packaged;
        else {
            DirectoryInfo? repo = new(AppContext.BaseDirectory);
            while (repo != null && !File.Exists(Path.Combine(repo.FullName, "neko_backend.py"))) repo = repo.Parent;
            if (repo == null) throw new FileNotFoundException("找不到控制后台，请使用完整发布包。");
            start.FileName = Path.Combine(repo.FullName, ".venv", "Scripts", "python.exe");
            start.ArgumentList.Add(Path.Combine(repo.FullName, "neko_backend.py"));
            start.WorkingDirectory = repo.FullName;
        }
        start.ArgumentList.Add("--pipe"); start.ArgumentList.Add(name);
        start.ArgumentList.Add("--stop-event"); start.ArgumentList.Add(@"Local\" + name + "-Stop");
        start.ArgumentList.Add("--frontend"); start.ArgumentList.Add(Environment.ProcessPath!);
        if (configDir != null) { start.ArgumentList.Add("--config-dir"); start.ArgumentList.Add(configDir); }
        _process = Process.Start(start) ?? throw new IOException("无法启动控制后台。");
        _process.ErrorDataReceived += (_, e) => { if (e.Data != null) Diagnostics = (Diagnostics + e.Data + "\n")[^Math.Min(Diagnostics.Length + e.Data.Length + 1, 6000)..]; };
        _process.BeginErrorReadLine();
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(25));
        try { await _pipe.WaitForConnectionAsync(timeout.Token); }
        catch { throw new IOException("控制后台未连接。" + Diagnostics); }
        _reader = new StreamReader(_pipe, new UTF8Encoding(false), false, 4096, true);
        _writer = new StreamWriter(_pipe, new UTF8Encoding(false), 4096, true) { AutoFlush = true };
    }

    public async Task<JsonObject> CallAsync(string method, JsonObject? data = null)
    {
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(35));
        await _gate.WaitAsync(timeout.Token);
        try {
            if (_writer == null || _reader == null) throw new IOException("后台未连接。");
            int id = ++_counter;
            string payload = new JsonObject { ["id"] = id, ["method"] = method, ["data"] = data?.DeepClone() }.ToJsonString();
            await _writer.WriteLineAsync(payload.AsMemory(), timeout.Token);
            string line = await _reader.ReadLineAsync(timeout.Token) ?? throw new IOException("后台连接已断开。" + Diagnostics);
            JsonObject response = JsonNode.Parse(line)!.AsObject();
            if (response["id"]?.GetValue<int>() != id) throw new IOException("后台响应序号错误。");
            if (response["ok"]?.GetValue<bool>() != true) throw new InvalidOperationException(response["error"]?.GetValue<string>() ?? "操作失败。");
            return response;
        }
        catch (OperationCanceledException) { _pipe?.Dispose(); throw new IOException("后台响应超时，已断开控制连接。"); }
        finally { _gate.Release(); }
    }

    public async ValueTask DisposeAsync()
    {
        _stopSignal?.Set();
        try { if (_pipe?.IsConnected == true) await CallAsync("shutdown").WaitAsync(TimeSpan.FromSeconds(6)); }
        catch { }
        _pipe?.Dispose();
        if (_process != null) {
            try { await _process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(8)); }
            catch { if (!_process.HasExited) _process.Kill(true); }
            _process.Dispose();
        }
        _stopSignal?.Dispose();
    }

    public void EmergencyStop() => _stopSignal?.Set();
}
