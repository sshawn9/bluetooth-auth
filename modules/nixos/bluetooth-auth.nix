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
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "alice";
      description = "User trusted by sudo auth and targeted by auto-lock.";
    };

    bluetoothAddress = lib.mkOption {
      type = lib.types.str;
      default = "";
      example = "AA:BB:CC:DD:EE:FF";
      description = "Bluetooth device address to monitor through BlueZ D-Bus.";
    };
  };

  config.assertions = [
    {
      assertion = !cfg.enable || cfg.bluetoothAddress != "";
      message = "my.security.bluetoothAuth.bluetoothAddress must be set when Bluetooth auth is enabled.";
    }
  ];
}
