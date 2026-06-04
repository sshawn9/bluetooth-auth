{
  config,
  lib,
  ...
}:

let
  cfg = config.my.security.bluetoothAuth;
  configFile = builtins.toFile "bluetooth-auth-config.json" (builtins.toJSON cfg.config);
in
{
  options.my.security.bluetoothAuth.autoConnect.enable =
    lib.mkEnableOption "automatic BlueZ Device1.Connect maintainer";

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
          configFile
        ];
        Restart = "always";
        RestartSec = "5s";
      };
    };
  };
}
