{
  config,
  lib,
  ...
}:

let
  cfg = config.my.security.bluetoothAuth;
  pamService = cfg.lockerAuth.pamService;
in
{
  options.my.security.bluetoothAuth.lockerAuth = {
    enable = lib.mkEnableOption "locker PAM authentication bypass when the Bluetooth device is connected";

    pamService = lib.mkOption {
      type = lib.types.str;
      default = "login";
      example = "login";
      description = "PAM service name used by the locker.";
    };

  };

  config = lib.mkIf (cfg.enable && cfg.lockerAuth.enable) {
    security.pam.services.${pamService}.rules.auth = import ./pam-auth.nix {
      inherit config lib;
      service = pamService;
      name = "bluetooth-auth-locker";
    };
  };
}
