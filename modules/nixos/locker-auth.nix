{
  config,
  lib,
  ...
}:

let
  cfg = config.my.security.bluetoothAuth;
  mkSettingsFile = import ./settings-file.nix;
  settingsFile = mkSettingsFile cfg;
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

    timeoutSeconds = lib.mkOption {
      type = lib.types.ints.between 1 2147483647;
      default = 2;
      description = "Maximum time to wait for the Bluetooth connection check during locker PAM auth.";
    };
  };

  config = lib.mkIf (cfg.enable && cfg.lockerAuth.enable) {
    security.pam.services.${pamService}.rules.auth.bluetooth-auth-locker = {
      order = (config.security.pam.services.${pamService}.rules.auth.unix.order or 11700) - 100;
      control = "sufficient";
      modulePath = "${config.security.pam.package}/lib/security/pam_exec.so";
      args = [
        "seteuid"
        "quiet"
        "${cfg.package}/bin/bluetooth-auth-oneshot-auth"
        settingsFile
        "locker"
      ];
    };
  };
}
