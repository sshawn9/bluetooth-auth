{
  config,
  lib,
  pkgs,
  utils,
  ...
}:

let
  cfg = config.my.security.bluetoothAuth;
in
{
  options.my.security.bluetoothAuth.autoConnect.enable =
    lib.mkEnableOption "automatic Bluetooth connection on startup, resume, and adapter power-on";

  config = lib.mkIf (cfg.enable && cfg.autoConnect.enable) {
    services.dbus.enable = true;
    services.dbus.packages = [
      (pkgs.writeTextDir "share/dbus-1/system.d/bluetooth-auth-power-monitor.conf" ''
        <busconfig>
          <policy user="root">
            <allow own="org.bluetooth_auth.PowerMonitor"/>
          </policy>
        </busconfig>
      '')
    ];

    systemd.services =
      lib.genAttrs
        [
          "systemd-suspend"
          "systemd-hibernate"
          "systemd-hybrid-sleep"
          "systemd-suspend-then-hibernate"
        ]
        (_: {
          onSuccess = [ "bluetooth-auth-connect.service" ];
        })
      // {
        bluetooth-auth-connect.wantedBy = [ "bluetooth.service" ];

        bluetooth-auth-power-monitor = {
          description = "Request a Bluetooth connection when the adapter is powered on";
          wantedBy = [ "bluetooth.service" ];
          before = [ "bluetooth.service" ];
          after = [
            "dbus.service"
            "systemd-tmpfiles-setup.service"
          ];
          serviceConfig = {
            # The program claims this name after subscribing, before BlueZ starts.
            Type = "dbus";
            BusName = "org.bluetooth_auth.PowerMonitor";
            ExecStart = utils.escapeSystemdExecArgs [
              "${cfg.package}/bin/bluetooth-auth-power-monitor"
              "--address-file"
              cfg.device.address.file
              "--timeout-ms"
              (toString cfg.connection.timeoutMs)
            ];
            Restart = "on-failure";
            RestartSec = "5s";
            TimeoutStartSec = "5s";
            TimeoutStopSec = "1s";
          };
        };
      };
  };
}
