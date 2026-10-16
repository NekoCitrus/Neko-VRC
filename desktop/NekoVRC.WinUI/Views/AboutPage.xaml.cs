using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Navigation;

namespace NekoVRC.WinUI.Views;

public sealed partial class AboutPage : Page
{
    public AboutPage() => InitializeComponent();
    protected override void OnNavigatedTo(NavigationEventArgs e)
    {
        base.OnNavigatedTo(e);
        DataContext = e.Parameter;
    }
}
