using System;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Automation;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Controls.Primitives;
using Microsoft.UI.Xaml.Data;
using Microsoft.UI.Xaml.Shapes;

namespace NekoVRC.WinUI.Controls;

// Extend, rather than replace, the official WinUI Slider template:
// https://github.com/microsoft/microsoft-ui-xaml/blob/main/controls/dev/CommonStyles/Slider_themeresources.xaml
// MediaTransportControls also uses this native Slider for its volume control.
public sealed class StrengthSlider : Slider
{
    public static readonly DependencyProperty CalculatedStrengthProperty = DependencyProperty.Register(
        nameof(CalculatedStrength), typeof(double), typeof(StrengthSlider),
        new PropertyMetadata(0d, (sender, _) => ((StrengthSlider)sender).UpdateLevel()));

    public double CalculatedStrength {
        get => (double)GetValue(CalculatedStrengthProperty);
        set => SetValue(CalculatedStrengthProperty, value);
    }

    internal Border? LevelBar { get; private set; }
    private Grid? _levelTrack;
    private Thumb? _thumb;

    public StrengthSlider() => DefaultStyleKey = typeof(Slider);

    protected override void OnApplyTemplate()
    {
        if (_levelTrack is not null) {
            _levelTrack.SizeChanged -= LevelSizeChanged;
            if (_levelTrack.Parent is Panel oldPanel) oldPanel.Children.Remove(_levelTrack);
        }
        if (_thumb is not null) _thumb.SizeChanged -= ThumbSizeChanged;
        _levelTrack = null;
        LevelBar = null;
        _thumb = null;
        base.OnApplyTemplate();

        if (GetTemplateChild("HorizontalTemplate") is not Grid horizontal ||
            GetTemplateChild("HorizontalDecreaseRect") is not Rectangle nativeFill ||
            GetTemplateChild("HorizontalThumb") is not Thumb thumb) return;

        _thumb = thumb;
        LevelBar = new Border {
            Name = "CalculatedStrengthLevel", Height = 6, CornerRadius = new CornerRadius(3),
            HorizontalAlignment = HorizontalAlignment.Left, IsHitTestVisible = false,
        };
        // Reuse the official accent fill, including its pointer/pressed/disabled
        // visual states and high-contrast theme, instead of a hard-coded color.
        LevelBar.SetBinding(Border.BackgroundProperty, new Binding { Source = nativeFill, Path = new PropertyPath("Fill") });
        AutomationProperties.SetName(LevelBar, "当前计算强度");
        _levelTrack = new Grid { Height = 6, VerticalAlignment = VerticalAlignment.Center, IsHitTestVisible = false };
        Grid.SetRowSpan(_levelTrack, 3);
        Grid.SetColumnSpan(_levelTrack, 3);
        _levelTrack.Children.Add(LevelBar);
        // Leave the original thumb above the meter, preserving its hit testing,
        // focus visuals, keyboard control, tooltip and native animations.
        horizontal.Children.Insert(horizontal.Children.IndexOf(thumb), _levelTrack);
        _levelTrack.SizeChanged += LevelSizeChanged;
        thumb.SizeChanged += ThumbSizeChanged;
        UpdateTrackInset();
    }

    private void LevelSizeChanged(object sender, SizeChangedEventArgs args) => UpdateLevel();
    private void ThumbSizeChanged(object sender, SizeChangedEventArgs args) => UpdateTrackInset();

    private void UpdateTrackInset()
    {
        if (_levelTrack is null || _thumb is null) return;
        // Start at the native track's left edge; the value endpoint follows
        // thumb-center travel, including after DPI/theme changes.
        _levelTrack.Margin = new Thickness(0, 0, _thumb.ActualWidth / 2, 0);
        UpdateLevel();
    }

    private void UpdateLevel()
    {
        if (LevelBar is null || _levelTrack is null) return;
        double fraction = double.IsFinite(CalculatedStrength) ? Math.Clamp(CalculatedStrength / 200, 0, 1) : 0;
        double inset = _thumb?.ActualWidth / 2 ?? 0;
        LevelBar.Width = fraction > 0 ? inset + Math.Max(0, _levelTrack.ActualWidth - inset) * fraction : 0;
        LevelBar.Visibility = fraction > 0 ? Visibility.Visible : Visibility.Collapsed;
    }
}
