using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Navigation;

namespace NekoVRC.WinUI.Views;

public sealed partial class DevicePage : Page
{
    public DevicePage() => InitializeComponent();
    protected override void OnNavigatedTo(NavigationEventArgs e)
    {
        base.OnNavigatedTo(e);
        // Pin the page to its own host. The outgoing page must not observe the
        // shell's CurrentDevice changing midway through its navigation animation.
        DataContext = ((MainViewModel)e.Parameter).CurrentDevice;
    }
}
