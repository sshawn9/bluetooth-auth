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
    ./auto-connect.nix
    ./noctalia-lock.nix
    ./auto-lock.nix
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

    bluetoothAddressFile = lib.mkOption {
      type = lib.types.str;
      default = "";
      example = lib.literalExpression "config.sops.secrets.auth_bluetooth_address.path";
      description = ''
        Runtime file containing the Bluetooth device address. Use this with
        secret managers such as sops-nix.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    my.security.bluetoothAuth.greetdAuth.enable = lib.mkForce false;
  };
}
