{
  config,
  lib,
  pkgs,
  utils,
  ...
}:

let
  cfg = config.my.security.bluetoothAuth;
  unlock = cfg.keyringUnlock;
in
{
  options.my.security.bluetoothAuth.keyringUnlock = {
    enable = lib.mkEnableOption "unlocking the GNOME login keyring with SOPS after a Bluetooth connection check";

    sopsFile = lib.mkOption {
      type = lib.types.path;
      description = "SOPS-encrypted file containing the existing login keyring password.";
    };

    sopsKey = lib.mkOption {
      type = lib.types.str;
      default = "login_keyring_password";
      description = "Top-level string key containing the password in sopsFile.";
    };

    ageKeyFile = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "/home/alice/.config/sops/age/keys.txt";
      description = "Runtime path to the user's age identity file. When unset, SOPS uses its normal key lookup.";
    };
  };

  config = lib.mkIf (cfg.enable && unlock.enable) {
    assertions = [
      {
        assertion = cfg.user != "";
        message = "Bluetooth keyring unlocking requires my.security.bluetoothAuth.user.";
      }
    ];

    systemd.user.services.bluetooth-auth-keyring-unlock = {
      description = "Unlock the login keyring when the Bluetooth auth device is connected";
      wantedBy = [ "graphical-session.target" ];
      after = [ "graphical-session.target" ];
      partOf = [ "graphical-session.target" ];
      unitConfig.ConditionUser = cfg.user;
      path = [ pkgs.sops ];
      environment = lib.optionalAttrs (unlock.ageKeyFile != null) {
        SOPS_AGE_KEY_FILE = unlock.ageKeyFile;
      };

      serviceConfig = {
        Type = "oneshot";
        ExecStart = utils.escapeSystemdExecArgs [
          "${cfg.package}/bin/bluetooth-auth-keyring-unlock"
          "--address-file"
          cfg.bluetoothAddressFile
          "--timeout-ms"
          (toString cfg.connect.timeoutMilliseconds)
          "--sops-file"
          "${unlock.sopsFile}"
          "--sops-key"
          unlock.sopsKey
        ];
        RemainAfterExit = true;
        TimeoutStartSec = "${toString (cfg.connect.timeoutMilliseconds + 15000)}ms";
      };
    };
  };
}
