using System.Windows.Controls;

namespace SimHub.Plugins.TrafficLight
{
    /// <summary>
    /// Interaction logic for SettingsControl.xaml
    /// </summary>
    public partial class SettingsControl : UserControl
    {
        public TrafficLightPlugin Plugin { get; }

        public SettingsControl()
        {
            InitializeComponent();
        }

        public SettingsControl(TrafficLightPlugin plugin) : this()
        {
            this.Plugin = plugin;

            // Bind the UI to the settings object
            UdpIpAddressTextBox.Text = Plugin.Settings.UdpIpAddress;
            UdpPortTextBox.Text = Plugin.Settings.UdpPort.ToString();
            RevLightsCheckBox.IsChecked = Plugin.Settings.UseRevLights;

            // Add event handlers to save settings when they change
            UdpIpAddressTextBox.TextChanged += (sender, args) => { Plugin.Settings.UdpIpAddress = UdpIpAddressTextBox.Text; };
            UdpPortTextBox.TextChanged += (sender, args) =>
            {
                if (int.TryParse(UdpPortTextBox.Text, out int port))
                {
                    Plugin.Settings.UdpPort = port;
                }
            };
            RevLightsCheckBox.Click += (sender, args) => { Plugin.Settings.UseRevLights = RevLightsCheckBox.IsChecked ?? false; };

            // Add event handlers for the test buttons
            SendRedButton.Click += (sender, e) => Plugin.SendUdpMessage("red");
            SendYellowButton.Click += (sender, e) => Plugin.SendUdpMessage("yellow");
            SendGreenButton.Click += (sender, e) => Plugin.SendUdpMessage("green");
            SendOffButton.Click += (sender, e) => Plugin.SendUdpMessage("black");
        }

        /// <summary>
        /// Public method to update the status text from the main plugin.
        /// </summary>
        public void UpdateStatus(string message)
        {
            StatusTextBlock.Text = message;
        }
    }
}
