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
  options.my.security.bluetoothAuth.device.address.sopsSecretName = lib.mkOption {
    type = lib.types.nullOr lib.types.str;
    default = null;
    example = "bluetooth_address";
    description = ''
      Name of the sops-nix secret containing the Bluetooth device address.
      Overrides device.address.file with its runtime path and grants accessGroup
      read access. Requires the sops-nix NixOS module.
    '';
  };

  config = lib.mkIf (cfg.enable && cfg.device.address.sopsSecretName != null) (
    {
      assertions = [
        {
          assertion = hasSops;
          message = "my.security.bluetoothAuth.device.address.sopsSecretName requires the sops-nix NixOS module.";
        }
      ];
    }
    // lib.optionalAttrs hasSops {
      my.security.bluetoothAuth.device.address.file =
        lib.mkForce
          config.sops.secrets.${cfg.device.address.sopsSecretName}.path;
      sops.secrets.${cfg.device.address.sopsSecretName} = {
        group = cfg.accessGroup;
        mode = "0440";
      };
    }
  );
}
