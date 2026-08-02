using SimHub.Plugins;
using System;
using System.Globalization;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Windows.Controls;
using GameReaderCommon;

namespace SimHub.Plugins.TrafficLight
{
    public class TrafficLightPluginSettings
    {
        public string UdpIpAddress { get; set; } = "127.0.0.1";
        public int UdpPort { get; set; } = 12345;
        public bool UseRevLights { get; set; } = false; // Setting for the new toggle
    }

    [PluginDescription("Sends game flag or rev light status via UDP.")]
    [PluginAuthor("You")]
    [PluginName("Traffic Light Plugin (Flags & Revs)")]
    public class TrafficLightPlugin : IPlugin, IDataPlugin, IWPFSettings
    {
        public TrafficLightPluginSettings Settings;
        private SettingsControl _settingsControl;

        private bool _initSequenceComplete = false;

        public PluginManager PluginManager { get; set; }

        public void Init(PluginManager pluginManager)
        {
            this.PluginManager = pluginManager;
            Settings = this.ReadCommonSettings("TrafficLightPluginSettings", () => new TrafficLightPluginSettings());

            // Run the initialization sequence
            new Thread(() =>
            {
                Thread.Sleep(500);
                SendUdpMessage("red:0");
                Thread.Sleep(250);
                SendUdpMessage("yellow:0");
                Thread.Sleep(250);
                SendUdpMessage("green:0");
                Thread.Sleep(250);
                SendUdpMessage("black:0");
                _initSequenceComplete = true;
            }).Start();
        }

        public void DataUpdate(PluginManager pluginManager, ref GameData data)
        {
            if (!_initSequenceComplete || !data.GameRunning || data.NewData == null) return;

            string currentState = "black"; // Default state

            // Same RPM values that drive the shift light thresholds below
            double currentRpms = GetPropertyDoubleSafe(pluginManager, "DataCorePlugin.GameData.Rpms");
            double redlineRpms = GetPropertyDoubleSafe(pluginManager, "DataCorePlugin.GameData.CarSettings_RPMRedLineReached");
            double shift2Rpms = GetPropertyDoubleSafe(pluginManager, "DataCorePlugin.GameData.CarSettings_RPMShiftLight2");
            double shift1Rpms = GetPropertyDoubleSafe(pluginManager, "DataCorePlugin.GameData.CarSettings_RPMShiftLight1");

            // CarSettings_RPMRedLineReached only populates once redline is actually hit (0 otherwise), so it
            // can't serve as a stable percentage denominator - CarSettings_RedLineRPM is the constant threshold.
            double stableRedlineRpms = GetPropertyDoubleSafe(pluginManager, "DataCorePlugin.GameData.CarSettings_RedLineRPM");
            double revPct = stableRedlineRpms > 0 ? Math.Max(0, Math.Min(100, currentRpms / stableRedlineRpms * 100)) : 0;

            // --- Check if the Rev Light mode is enabled ---
            if (Settings.UseRevLights)
            {
                string diagnosticStatus = $"Mode: Revs | RPM:{Math.Round(currentRpms)} R:{redlineRpms} Y:{shift2Rpms} G:{shift1Rpms}";
                _settingsControl?.Dispatcher.Invoke(() => _settingsControl.UpdateStatus(diagnosticStatus));

                if (redlineRpms > 0 && currentRpms >= redlineRpms) { currentState = "red"; }
                else if (shift2Rpms > 0 && currentRpms >= shift2Rpms) { currentState = "yellow"; }
                else if (shift1Rpms > 0 && currentRpms >= shift1Rpms) { currentState = "green"; }
            }
            else
            {
                // --- Flag and Race Start Logic ---
                bool isRaceStartSequenceActive = false;
                if (data.GameName == "IRacing")
                {
                    if (Convert.ToBoolean(pluginManager.GetPropertyValue("DataCorePlugin.GameRawData.Telemetry.SessionFlagsDetails.IsstartGo"))) { currentState = "green"; isRaceStartSequenceActive = true; }
                    else if (Convert.ToBoolean(pluginManager.GetPropertyValue("DataCorePlugin.GameRawData.Telemetry.SessionFlagsDetails.IsstartSet")) || Convert.ToBoolean(pluginManager.GetPropertyValue("DataCorePlugin.GameRawData.Telemetry.SessionFlagsDetails.Isfurled"))) { currentState = "red"; isRaceStartSequenceActive = true; }
                    else if (Convert.ToBoolean(pluginManager.GetPropertyValue("DataCorePlugin.GameRawData.SessionData.WeekendInfo.WeekendOptions.StandingStart")) && Convert.ToBoolean(pluginManager.GetPropertyValue("DataCorePlugin.GameRawData.Telemetry.SessionFlagsDetails.IsstartReady"))) { currentState = "yellow"; isRaceStartSequenceActive = true; }
                }
                if (!isRaceStartSequenceActive)
                {
                    if (data.NewData.Flag_Yellow > 0) { currentState = "yellow"; }
                    else if (data.NewData.Flag_Green > 0) { currentState = "green"; }
                }
                _settingsControl?.Dispatcher.Invoke(() => _settingsControl.UpdateStatus($"Mode: Flags | Current State: {currentState}"));
            }

            // --- New Progressive Logic for Rev Lights ---
            string messageToSend;
            if (Settings.UseRevLights)
            {
                messageToSend = "black"; // Default to turning lights off
                if (currentState == "green")
                {
                    messageToSend = "green";
                }
                else if (currentState == "yellow")
                {
                    messageToSend = "green-yellow";
                }
                else if (currentState == "red")
                {
                    messageToSend = "all_on";
                }
            }
            else // For flags, just send the single state
            {
                messageToSend = currentState;
            }

            // Sent every tick (not just on state change) so the rev_pct bar never freezes on a stale value
            SendUdpMessage($"{messageToSend}:{revPct.ToString("0.#", CultureInfo.InvariantCulture)}");
        }

        private double GetPropertyDoubleSafe(PluginManager pluginManager, string propertyName)
        {
            try
            {
                object value = pluginManager.GetPropertyValue(propertyName);
                return value == null ? 0 : Convert.ToDouble(value);
            }
            catch
            {
                return 0;
            }
        }

        public void SendUdpMessage(string message)
        {
            try
            {
                using (var client = new UdpClient())
                {
                    byte[] dataBytes = Encoding.UTF8.GetBytes(message);
                    client.Send(dataBytes, dataBytes.Length, Settings.UdpIpAddress, Settings.UdpPort);
                    SimHub.Logging.Current.Info($"Traffic Light Plugin: Sent UDP packet: {message}");
                }
            }
            catch (Exception ex)
            {
                SimHub.Logging.Current.Error($"Traffic Light Plugin: Failed to send UDP packet. {ex.Message}");
                _settingsControl?.Dispatcher.Invoke(() => _settingsControl.UpdateStatus($"Error: {ex.Message}"));
            }
        }

        public void End(PluginManager pluginManager)
        {
            this.SaveCommonSettings("TrafficLightPluginSettings", Settings);
        }

        public Control GetWPFSettingsControl(PluginManager pluginManager)
        {
            _settingsControl = new SettingsControl(this);
            return _settingsControl;
        }
    }
}
