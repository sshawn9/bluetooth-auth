{
  config,
  lib,
  options,
  ...
}:
let
  cfg = config.my.security.bluetoothAuth;
  hasSops = options ? sops.secrets;
in
{
  options.my.security.bluetoothAuth.sopsSecret = lib.mkOption {
    type = lib.types.nullOr lib.types.str;
    default = null;
    example = "bluetooth_address";
    description = ''
      Name of the sops-nix secret containing the Bluetooth device address.
      Overrides bluetoothAddressFile with its runtime path and grants the configured
      group read access. Requires the sops-nix NixOS module.
    '';
  };

  config = lib.mkIf (cfg.enable && cfg.sopsSecret != null) (
    {
      assertions = [
        {
          assertion = hasSops;
          message = "my.security.bluetoothAuth.sopsSecret requires the sops-nix NixOS module.";
        }
      ];
    }
    // lib.optionalAttrs hasSops {
      my.security.bluetoothAuth.bluetoothAddressFile =
        lib.mkForce
          config.sops.secrets.${cfg.sopsSecret}.path;
      sops.secrets.${cfg.sopsSecret} = {
        group = cfg.group;
        mode = "0440";
      };
    }
  );
}
