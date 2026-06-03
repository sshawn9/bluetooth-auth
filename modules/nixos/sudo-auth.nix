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
  options.my.security.bluetoothAuth.sudoAuth.enable =
    (lib.mkEnableOption "sudo authentication bypass when the Bluetooth device is connected")
    // {
      default = true;
    };

  config = lib.mkIf (cfg.enable && cfg.sudoAuth.enable) {
    security.pam.services.sudo.rules.auth.bluetooth-auth = {
      order = config.security.pam.services.sudo.rules.auth.unix.order - 100;
      control = "sufficient";
      modulePath = "${config.security.pam.package}/lib/security/pam_exec.so";
      args = [
        "seteuid"
        "quiet"
        "${cfg.package}/bin/bluetooth-auth-sudo-auth"
        configFile
      ];
    };
  };
}
