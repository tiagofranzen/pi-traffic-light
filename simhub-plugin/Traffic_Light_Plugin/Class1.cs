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
    }

    [PluginDescription("Sends rev lights, race start/flags and hotlap delta to the traffic light via UDP.")]
    [PluginAuthor("You")]
    [PluginName("Traffic Light Plugin (Flags & Revs)")]
    public class TrafficLightPlugin : IPlugin, IDataPlugin, IWPFSettings
    {
        public TrafficLightPluginSettings Settings;
        private SettingsControl _settingsControl;

        private bool _initSequenceComplete = false;
        private readonly UdpClient _udp = new UdpClient();
        private DateTime _lastStatusUpdate = DateTime.MinValue;

        // Hotlap (iRacing): the current lap stays invalid from the first infraction until the next lap starts.
        private int _lapNumber = -1;
        private bool _lapInvalid = false;

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

            // Everything is sent on every tick; the traffic light's Racing sub-mode picks what to show.
            double currentRpms = GetPropertyDoubleSafe(pluginManager, "DataCorePlugin.GameData.Rpms");
            // CarSettings_RPMRedLineReached only populates once redline is actually hit (0 otherwise), so it
            // can't serve as a stable percentage denominator - CarSettings_RedLineRPM is the constant threshold.
            double stableRedlineRpms = GetPropertyDoubleSafe(pluginManager, "DataCorePlugin.GameData.CarSettings_RedLineRPM");
            double revPct = stableRedlineRpms > 0 ? Math.Max(0, Math.Min(100, currentRpms / stableRedlineRpms * 100)) : 0;
            string revState = ReadRevState(pluginManager, currentRpms);
            string flagState = ReadFlagState(pluginManager, data);
            double? delta = ReadLapDelta(pluginManager, data);
            bool lapInvalid = UpdateLapInvalid(pluginManager, data);
            string deltaText = delta.HasValue ? delta.Value.ToString("0.000", CultureInfo.InvariantCulture) : "";

            // Format: <rev_state>:<rev_pct>:<delta>:<invalid>:<flag_state>
            // Sent every tick (not just on change) so the rev bar and delta never freeze on a stale value.
            SendUdpMessage($"{revState}:{revPct.ToString("0.#", CultureInfo.InvariantCulture)}:{deltaText}:{(lapInvalid ? 1 : 0)}:{flagState}");

            // The settings page only needs a few refreshes a second.
            if (_settingsControl != null && (DateTime.UtcNow - _lastStatusUpdate).TotalMilliseconds > 250)
            {
                _lastStatusUpdate = DateTime.UtcNow;
                string status = $"Revs: {revState} ({Math.Round(revPct)}%)\n" +
                                $"Start/flags: {flagState}\n" +
                                (delta.HasValue ? $"Delta: {deltaText} s" : "Delta: no reference lap") + $" | Lap {(lapInvalid ? "INVALID" : "valid")}" +
                                $"\nGame: {data.GameName} | lap {data.NewData.CurrentLap} @ {data.NewData.CurrentLapTime.TotalSeconds:0.0} s | game says valid: {data.NewData.IsLapValid}";
                _settingsControl.Dispatcher.BeginInvoke(new Action(() => _settingsControl.UpdateStatus(status)));
            }
        }

        // Shift lights: green, then green + yellow, then all three at redline.
        private string ReadRevState(PluginManager pluginManager, double currentRpms)
        {
            double redlineRpms = GetPropertyDoubleSafe(pluginManager, "DataCorePlugin.GameData.CarSettings_RPMRedLineReached");
            double shift2Rpms = GetPropertyDoubleSafe(pluginManager, "DataCorePlugin.GameData.CarSettings_RPMShiftLight2");
            double shift1Rpms = GetPropertyDoubleSafe(pluginManager, "DataCorePlugin.GameData.CarSettings_RPMShiftLight1");
            if (redlineRpms > 0 && currentRpms >= redlineRpms) return "all_on";
            if (shift2Rpms > 0 && currentRpms >= shift2Rpms) return "green-yellow";
            if (shift1Rpms > 0 && currentRpms >= shift1Rpms) return "green";
            return "black";
        }

        // Race start lights (iRacing), otherwise the yellow/green flag.
        private string ReadFlagState(PluginManager pluginManager, GameData data)
        {
            if (data.GameName == "IRacing")
            {
                const string flags = "DataCorePlugin.GameRawData.Telemetry.SessionFlagsDetails.";
                if (GetPropertyBoolSafe(pluginManager, flags + "IsstartGo")) return "green";
                if (GetPropertyBoolSafe(pluginManager, flags + "IsstartSet") || GetPropertyBoolSafe(pluginManager, flags + "Isfurled")) return "red";
                if (GetPropertyBoolSafe(pluginManager, "DataCorePlugin.GameRawData.SessionData.WeekendInfo.WeekendOptions.StandingStart")
                    && GetPropertyBoolSafe(pluginManager, flags + "IsstartReady")) return "yellow";
            }
            if (data.NewData.Flag_Yellow > 0) return "yellow";
            if (data.NewData.Flag_Green > 0) return "green";
            return "black";
        }

        private double? ReadLapDelta(PluginManager pluginManager, GameData data)
        {
            // iRacing's own delta to your best lap, only when iRacing says it's usable.
            if (data.GameName == "IRacing")
            {
                if (GetPropertyBoolSafe(pluginManager, "DataCorePlugin.GameRawData.Telemetry.LapDeltaToBestLap_OK"))
                    return GetPropertyDoubleSafe(pluginManager, "DataCorePlugin.GameRawData.Telemetry.LapDeltaToBestLap");
                return null;
            }
            // Other games: SimHub's own live delta to the session best.
            return data.NewData.DeltaToSessionBest;
        }

        private bool UpdateLapInvalid(PluginManager pluginManager, GameData data)
        {
            // Other games keep their own per-lap validity flag until the line, so use it live. Latching it here
            // (or using SimHub's latched LapInvalidated) re-arms on the stale "invalid" the game still reports for
            // a tick or two after the line, which kept the whole next lap red (seen in AC EVO).
            if (data.GameName != "IRacing") return !data.NewData.IsLapValid;

            int lap = (int)GetPropertyDoubleSafe(pluginManager, "DataCorePlugin.GameRawData.Telemetry.Lap");
            if (lap != _lapNumber)
            {
                _lapNumber = lap;
                _lapInvalid = false; // a new lap starts clean
            }

            bool infraction = data.NewData.LapInvalidated;
            // Track limits: the player's car is off the racing surface.
            int carIdx = (int)GetPropertyDoubleSafe(pluginManager, "DataCorePlugin.GameRawData.SessionData.DriverInfo.DriverCarIdx");
            if (pluginManager.GetPropertyValue("DataCorePlugin.GameRawData.Telemetry.CarIdxTrackSurface") is Array surfaces
                && carIdx >= 0 && carIdx < surfaces.Length
                && Convert.ToString(surfaces.GetValue(carIdx)) == "OffTrack")
            {
                infraction = true;
            }
            // Penalties: black flag, black flag warning, meatball (repair) or disqualification.
            foreach (string flag in new[] { "Isblack", "Isfurled", "Isrepair", "Isdisqualify" })
            {
                if (GetPropertyBoolSafe(pluginManager, "DataCorePlugin.GameRawData.Telemetry.SessionFlagsDetails." + flag))
                    infraction = true;
            }

            if (infraction) _lapInvalid = true;
            return _lapInvalid;
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

        private bool GetPropertyBoolSafe(PluginManager pluginManager, string propertyName)
        {
            try
            {
                object value = pluginManager.GetPropertyValue(propertyName);
                return value != null && Convert.ToBoolean(value);
            }
            catch
            {
                return false;
            }
        }

        public void SendUdpMessage(string message)
        {
            try
            {
                // One socket for the plugin's lifetime; sending ~60 packets a second, a new client per packet adds up.
                byte[] dataBytes = Encoding.UTF8.GetBytes(message);
                _udp.Send(dataBytes, dataBytes.Length, Settings.UdpIpAddress, Settings.UdpPort);
            }
            catch (Exception ex)
            {
                SimHub.Logging.Current.Error($"Traffic Light Plugin: Failed to send UDP packet. {ex.Message}");
                _settingsControl?.Dispatcher.BeginInvoke(new Action(() => _settingsControl.UpdateStatus($"Error: {ex.Message}")));
            }
        }

        public void End(PluginManager pluginManager)
        {
            this.SaveCommonSettings("TrafficLightPluginSettings", Settings);
            _udp.Close();
        }

        public Control GetWPFSettingsControl(PluginManager pluginManager)
        {
            _settingsControl = new SettingsControl(this);
            return _settingsControl;
        }
    }
}
