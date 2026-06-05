{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.my.security.bluetoothAuth;
  mkSettingsFile = import ./settings-file.nix;
  settingsFile = mkSettingsFile cfg;
in
{
  options.my.security.bluetoothAuth.autoLock = {
    enable = lib.mkEnableOption "automatic locking when the Bluetooth auth device is disconnected";

    checkIntervalSeconds = lib.mkOption {
      type = lib.types.ints.between 1 2147483647;
      default = 40;
      description = "How often to query BlueZ Connected and check the session lock state.";
    };

    sleepAfterLockSeconds = lib.mkOption {
      type = lib.types.ints.between 30 2147483647;
      default = 30;
      description = ''
        How long to wait after confirming the session is locked. This gives
        the user a grace window after manually unlocking, for example to stop
        auto-lock when the Bluetooth device is still unavailable.
      '';
    };

    exceptionGraceSeconds = lib.mkOption {
      type = lib.types.ints.between 1 2147483647;
      default = 300;
      description = "How long to wait before retrying after a BlueZ or D-Bus error.";
    };
  };

  config = lib.mkIf (cfg.enable && cfg.autoLock.enable) {
    systemd.services.bluetooth-auth-auto-lock = {
      description = "Lock the session when the Bluetooth auth device is disconnected";
      wantedBy = [ "graphical.target" ];
      wants = [ "bluetooth.service" ];
      after = [
        "graphical.target"
        "bluetooth.service"
      ];
      path = [ pkgs.systemd ];

      serviceConfig = {
        Type = "simple";
        ExecStart = lib.escapeShellArgs [
          "${cfg.package}/bin/bluetooth-auth-auto-lock"
          settingsFile
        ];
        Restart = "always";
        RestartSec = "5s";
      };
    };
  };
}
