{
  config,
  lib,
  ...
}:

let
  cfg = config.my.security.bluetoothAuth;
  configFile = builtins.toFile "bluetooth-auth-config.json" (builtins.toJSON cfg.config);
  pamService = cfg.greetdAuth.pamService;
in
{
  options.my.security.bluetoothAuth.greetdAuth = {
    enable = lib.mkEnableOption "greetd PAM authentication bypass when the Bluetooth device is connected";

    pamService = lib.mkOption {
      type = lib.types.str;
      default = "greetd";
      example = "greetd";
      description = "PAM service name used by greetd.";
    };
  };

  config = lib.mkIf (cfg.enable && cfg.greetdAuth.enable) {
    security.pam.services.${pamService}.rules.auth.bluetooth-auth-greetd = {
      order = (config.security.pam.services.${pamService}.rules.auth.unix.order or 11700) - 100;
      control = "sufficient";
      modulePath = "${config.security.pam.package}/lib/security/pam_exec.so";
      args = [
        "seteuid"
        "quiet"
        "${cfg.package}/bin/bluetooth-auth-oneshot-auth"
        configFile
        "greetd"
      ];
    };
  };
}
