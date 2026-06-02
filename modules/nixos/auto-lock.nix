{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.my.security.bluetoothAuth;
  user = if cfg.user == null then "" else cfg.user;
in
{
  options.my.security.bluetoothAuth.autoLock = {
    enable =
      (lib.mkEnableOption "automatic locking when the Bluetooth auth device is disconnected")
      // {
        default = true;
      };

    checkIntervalSeconds = lib.mkOption {
      type = lib.types.ints.between 1 2147483647;
      default = 30;
      description = "How often to query BlueZ Connected and retry lock checks.";
    };
  };

  config = lib.mkIf (cfg.enable && cfg.autoLock.enable) {
    assertions = [
      {
        assertion = cfg.user != null;
        message = "my.security.bluetoothAuth.user must be set for autoLock.";
      }
    ];

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
          user
          cfg.bluetoothAddress
          (toString cfg.autoLock.checkIntervalSeconds)
        ];
        Restart = "always";
        RestartSec = "5s";
      };
    };
  };
}
