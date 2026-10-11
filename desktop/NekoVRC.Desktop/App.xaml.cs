using System;
using System.Threading;
using System.Windows;

namespace NekoVRC.Desktop;

public partial class App : Application
{
    private Mutex? _mutex;
    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        _mutex = new Mutex(true, @"Local\NekoVRC.SingleInstance", out bool created);
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
