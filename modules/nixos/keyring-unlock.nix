{
  config,
  lib,
  pkgs,
  utils,
  ...
}:

let
  cfg = config.my.security.bluetoothAuth;
  unlock = cfg.gnomeKeyringUnlock;
in
{
  options.my.security.bluetoothAuth.gnomeKeyringUnlock = {
    enable = lib.mkEnableOption "unlocking the GNOME login keyring with SOPS after a Bluetooth connection check";

    password = {
      sopsFile = lib.mkOption {
        type = lib.types.path;
        description = "SOPS-encrypted file containing the existing login keyring password.";
      };

      sopsField = lib.mkOption {
        type = lib.types.str;
        default = "login_keyring_password";
        description = "Top-level string field containing the password in password.sopsFile.";
      };

      ageKeyFile = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "/home/alice/.config/sops/age/keys.txt";
        description = "Runtime path to the user's age identity file for decrypting password.sopsFile. When unset, SOPS uses its normal key lookup.";
      };
    };
  };

  config = lib.mkIf (cfg.enable && unlock.enable) {
    assertions = [
      {
        assertion = cfg.trustedUser != "";
        message = "Bluetooth keyring unlocking requires my.security.bluetoothAuth.trustedUser.";
      }
    ];

    systemd.user.services.bluetooth-auth-keyring-unlock = {
      description = "Unlock the login keyring when the Bluetooth auth device is connected";
      wantedBy = [ "graphical-session.target" ];
      after = [ "graphical-session.target" ];
      partOf = [ "graphical-session.target" ];
      unitConfig.ConditionUser = cfg.trustedUser;
      path = [ pkgs.sops ];
      environment = lib.optionalAttrs (unlock.password.ageKeyFile != null) {
        SOPS_AGE_KEY_FILE = unlock.password.ageKeyFile;
      };

      serviceConfig = {
        Type = "oneshot";
        ExecStart = utils.escapeSystemdExecArgs [
          "${cfg.package}/bin/bluetooth-auth-keyring-unlock"
          "--address-file"
          cfg.device.address.file
          "--timeout-ms"
          (toString cfg.connection.timeoutMs)
          "--sops-file"
          "${unlock.password.sopsFile}"
          "--sops-key"
          unlock.password.sopsField
        ];
        RemainAfterExit = true;
        TimeoutStartSec = "${toString (cfg.connection.timeoutMs + 15000)}ms";
      };
    };
  };
}
