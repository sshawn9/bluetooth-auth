{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.my.security.bluetoothAuth;
  configFile = builtins.toFile "bluetooth-auth-config.json" (builtins.toJSON cfg.config);
in
{
  options.my.security.bluetoothAuth.autoLock.enable =
    (lib.mkEnableOption "automatic locking when the Bluetooth auth device is disconnected")
    // {
      default = true;
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
          configFile
        ];
        Restart = "always";
        RestartSec = "5s";
      };
    };
  };
}
