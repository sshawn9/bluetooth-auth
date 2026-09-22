{
  config,
  lib,
  ...
}:

let
  cfg = config.my.security.bluetoothAuth;
  helper = "${cfg.package}/bin/bluetooth-auth-link";
  trustedUser = builtins.toJSON cfg.trustedUser;
in
{
  options.my.security.bluetoothAuth.auth.polkit = {
    enable = lib.mkEnableOption "polkit authentication bypass when the Bluetooth device is connected";
  };

  config = lib.mkIf (cfg.enable && cfg.auth.polkit.enable) {
    security.polkit.enable = true;

    # The Rust checker reads the host HCI connection table directly.
    systemd.services.polkit.serviceConfig = {
      PrivateNetwork = false;
      RestrictAddressFamilies = [
        "AF_UNIX"
        "AF_BLUETOOTH"
      ];
    };

    security.polkit.extraConfig = ''
      polkit.addRule(function(action, subject) {
        if (
          !subject.user ||
          subject.user != ${trustedUser} ||
          !subject.local ||
          !subject.active
        ) {
          return polkit.Result.NOT_HANDLED;
        }

        try {
          polkit.spawn([
            ${builtins.toJSON helper},
            "--address-file",
            ${builtins.toJSON cfg.device.address.file},
            "--connect",
            "-1"
          ]);
          return polkit.Result.YES;
        } catch (error) {
          return polkit.Result.NOT_HANDLED;
        }
      });
    '';
  };
}
