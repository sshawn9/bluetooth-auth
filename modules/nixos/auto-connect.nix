{
  config,
  lib,
  ...
}:

let
  cfg = config.my.security.bluetoothAuth;
  mkSettingsFile = import ./settings-file.nix;
  settingsFile = mkSettingsFile cfg;
in
{
  options.my.security.bluetoothAuth.autoConnect = {
    enable = lib.mkEnableOption "automatic BlueZ Device1.Connect maintainer";

    checkIntervalSeconds = lib.mkOption {
      type = lib.types.ints.between 1 2147483647;
      default = 30;
      description = "How often to check whether the Bluetooth device is already connected.";
    };

    deviceUnvailableGraceSeconds = lib.mkOption {
      type = lib.types.ints.between 1 2147483647;
      default = 300;
      description = "How long to wait when the adapter is off or the device is unavailable.";
    };

    exceptionGraceSeconds = lib.mkOption {
      type = lib.types.ints.between 1 2147483647;
      default = 300;
      description = "How long to wait before retrying after a BlueZ or D-Bus error.";
    };

    reconnectTimes = lib.mkOption {
      type = lib.types.ints.between 1 2147483647;
      default = 5;
      description = "How many times to retry BlueZ Device1.Connect before rechecking device state.";
    };

    reconnectIntervalSeconds = lib.mkOption {
      type = lib.types.ints.between 1 2147483647;
      default = 6;
      description = "How long to wait between BlueZ Device1.Connect retries.";
    };
  };

  config = lib.mkIf (cfg.enable && cfg.autoConnect.enable) {
    systemd.services.bluetooth-auth-auto-connect = {
      description = "Maintain a BlueZ device connection";
      wantedBy = [ "multi-user.target" ];
      requires = [ "bluetooth.service" ];
      after = [ "bluetooth.service" ];
      before = [
        "greetd.service"
        "display-manager.service"
      ];

      serviceConfig = {
        Type = "simple";
        ExecStart = lib.escapeShellArgs [
          "${cfg.package}/bin/bluetooth-auth-auto-connect"
          settingsFile
        ];
        Restart = "always";
        RestartSec = "5s";
      };
    };
  };
}
