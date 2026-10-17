using System;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using Microsoft.UI.Xaml;

namespace NekoVRC.WinUI;

public partial class App : Application
{
    private Mutex? _mutex;
    public static MainWindow? CurrentWindow { get; private set; }
    public static string[] Arguments { get; } = Environment.GetCommandLineArgs().Skip(1).ToArray();
    public static string? TestDirectory => Arguments.Length == 2 && Arguments[0] is "--smoke-test" or "--smoke-test-close" or "--smoke-waveforms" or "--preview-ui" ? Path.GetFullPath(Arguments[1]) : null;
    public App()
    {
        InitializeComponent();
        UnhandledException += (_, e) => {
            e.Handled = true;
            if (CurrentWindow != null) CurrentWindow.ReportFailure(e.Exception);
            else {
                string folder = TestDirectory ?? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "Neko-VRC");
                Directory.CreateDirectory(folder);
                File.WriteAllText(Path.Combine(folder, "winui-startup-error.txt"), e.Exception.ToString());
                Environment.ExitCode = 1; Exit();
            }
        };
    }
    protected override void OnLaunched(LaunchActivatedEventArgs args)
    {
        string mutexName = @"Local\NekoVRC.SingleInstance";
        if (TestDirectory != null)
            mutexName += ".WinUITest." + Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(TestDirectory.ToUpperInvariant())))[..16];
        _mutex = new Mutex(true, mutexName, out bool created);
        if (!created) { Exit(); return; }
        CurrentWindow = new MainWindow();
        CurrentWindow.Closed += (_, _) => { _mutex?.Dispose(); CurrentWindow = null; };
        CurrentWindow.Activate();
    }
}
