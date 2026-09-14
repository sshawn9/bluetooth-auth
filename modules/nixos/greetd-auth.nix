{
  config,
  lib,
  ...
}:

let
  cfg = config.my.security.bluetoothAuth;
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
    security.pam.services.${pamService}.rules.auth = import ./pam-auth.nix {
      inherit config lib;
      service = pamService;
      name = "bluetooth-auth-greetd";
    };
  };
}
