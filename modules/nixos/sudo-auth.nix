{
  config,
  lib,
  ...
}:

let
  cfg = config.my.security.bluetoothAuth;
  mkSettingsFile = import ./settings-file.nix;
  settingsFile = mkSettingsFile cfg;
in
{
  options.my.security.bluetoothAuth.sudoAuth = {
    enable = lib.mkEnableOption "sudo authentication bypass when the Bluetooth device is connected";

    timeoutSeconds = lib.mkOption {
      type = lib.types.ints.between 1 2147483647;
      default = 2;
      description = "Maximum time to wait for the Bluetooth connection check during sudo PAM auth.";
    };
  };

  config = lib.mkIf (cfg.enable && cfg.sudoAuth.enable) {
    security.pam.services.sudo.rules.auth.bluetooth-auth = {
      order = config.security.pam.services.sudo.rules.auth.unix.order - 100;
      control = "sufficient";
      modulePath = "${config.security.pam.package}/lib/security/pam_exec.so";
      args = [
        "seteuid"
        "quiet"
        "${cfg.package}/bin/bluetooth-auth-oneshot-auth"
        settingsFile
        "sudo"
      ];
    };
  };
}
