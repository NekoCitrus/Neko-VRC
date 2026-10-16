using System;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Windows;

namespace NekoVRC.Desktop;

public partial class App : Application
{
    private Mutex? _mutex;
    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        string mutexName = @"Local\NekoVRC.SingleInstance";
        if (e.Args.Length == 2 && e.Args[0] is "--smoke-test" or "--preview-ui")
            mutexName += ".Test." + Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(Path.GetFullPath(e.Args[1]))))[..16];
        _mutex = new Mutex(true, mutexName, out bool created);
        if (!created) { Shutdown(); return; }
        DispatcherUnhandledException += (_, args) => {
            MessageBox.Show(args.Exception.Message, "Neko-VRC"); args.Handled = true;
        };
        var window = new MainWindow(e.Args);
        MainWindow = window;
        window.Show();
    }
    protected override void OnExit(ExitEventArgs e)
    {
        _mutex?.Dispose();
        base.OnExit(e);
    }
}
