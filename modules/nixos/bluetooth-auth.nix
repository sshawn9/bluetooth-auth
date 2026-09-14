{
  bluetoothAuthPackage,
  config,
  lib,
  ...
}:
let
  cfg = config.my.security.bluetoothAuth;
in
{
  imports = [
    ./sops.nix
    ./keyring-unlock.nix
    ./connect.nix
    ./noctalia-auto-lock.nix
    ./sudo-auth.nix
    ./polkit-auth.nix
    ./locker-auth.nix
    ./greetd-auth.nix
  ];

  options.my.security.bluetoothAuth = {
    enable = lib.mkEnableOption "Enable Bluetooth authentication services.";

    package = lib.mkOption {
      type = lib.types.package;
      default = bluetoothAuthPackage;
      defaultText = lib.literalExpression "inputs.bluetooth-auth.packages.<system>.bluetooth-auth";
      description = "Package that provides the bluetooth-auth command-line tools.";
    };

    user = lib.mkOption {
      type = lib.types.str;
      default = "";
      example = "alice";
      description = "User trusted by sudo, polkit, and PAM auth and targeted by auto-lock.";
    };

    group = lib.mkOption {
      type = lib.types.str;
      default = "bluetooth-auth-connect";
      description = ''
        Group with access to the connection socket and lock file. Address-file
        permissions can also use this group. It includes the configured user
        and, when polkitAuth is enabled, polkituser.
      '';
    };

    bluetoothAddressFile = lib.mkOption {
      type = lib.types.str;
      default = "";
      example = lib.literalExpression "config.sops.secrets.auth_bluetooth_address.path";
      description = ''
        Runtime file containing the Bluetooth device address. Use this with
        secret managers such as sops-nix.
        When noctaliaAutoLock is enabled, the configured user must be able to
        read this file. When polkitAuth is enabled, polkituser must also be able
        to read it.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    environment.systemPackages = [ cfg.package ];
    users.groups.${cfg.group}.members =
      lib.optional (cfg.user != "") cfg.user ++ lib.optional cfg.polkitAuth.enable "polkituser";
  };
}
