{
  config,
  lib,
  ...
}:

let
  cfg = config.my.security.bluetoothAuth;
in
{
  options.my.security.bluetoothAuth.auth.sudo = {
    enable = lib.mkEnableOption "sudo authentication bypass when the Bluetooth device is connected";
  };

  config = lib.mkIf (cfg.enable && cfg.auth.sudo.enable) {
    security.pam.services.sudo.rules.auth = import ./pam-auth.nix {
      inherit config lib;
      service = "sudo";
      name = "bluetooth-auth";
      allowRuser = true;
    };
  };
}
