{
  config,
  lib,
  ...
}:

let
  cfg = config.my.security.bluetoothAuth;
  pamService = cfg.auth.greetd.pamService;
in
{
  options.my.security.bluetoothAuth.auth.greetd = {
    enable = lib.mkEnableOption "greetd PAM authentication bypass when the Bluetooth device is connected";

    pamService = lib.mkOption {
      type = lib.types.str;
      default = "greetd";
      example = "greetd";
      description = "PAM service name used by greetd.";
    };
  };

  config = lib.mkIf (cfg.enable && cfg.auth.greetd.enable) {
    security.pam.services.${pamService}.rules.auth = import ./pam-auth.nix {
      inherit config lib;
      service = pamService;
      name = "bluetooth-auth-greetd";
    };
  };
}
