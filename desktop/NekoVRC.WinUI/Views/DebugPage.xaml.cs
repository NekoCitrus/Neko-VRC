using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Navigation;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Media;

namespace NekoVRC.WinUI.Views;

public sealed partial class DebugPage : Page
{
    public DebugPage() => InitializeComponent();
    private void LogTextChanged(object sender, TextChangedEventArgs e) => FollowLatest((TextBox)sender);
    private void LogLoaded(object sender, RoutedEventArgs e) => FollowLatest((TextBox)sender);
    private static void FollowLatest(TextBox textBox)
    {
        textBox.DispatcherQueue.TryEnqueue(() => {
            if (textBox.XamlRoot == null) return;
            textBox.UpdateLayout();
            textBox.SelectionStart = textBox.Text.Length;
            var scroll = FindScroll(textBox);
            scroll?.ChangeView(null, scroll.ScrollableHeight, null, true);
        });
    }
    private static ScrollViewer? FindScroll(DependencyObject root)
    {
        for (int i = 0; i < VisualTreeHelper.GetChildrenCount(root); i++) {
            var child = VisualTreeHelper.GetChild(root, i);
            if (child is ScrollViewer scroll) return scroll;
            var nested = FindScroll(child);
            if (nested != null) return nested;
        }
        return null;
    }
    protected override void OnNavigatedTo(NavigationEventArgs e)
    {
        base.OnNavigatedTo(e);
        DataContext = e.Parameter;
    }
}
