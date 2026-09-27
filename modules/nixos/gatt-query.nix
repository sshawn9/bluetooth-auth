{
  config,
  lib,
  utils,
  ...
}:

let
  cfg = config.my.security.bluetoothAuth;
  query = cfg.gattQuery;
in
{
  options.my.security.bluetoothAuth.gattQuery = {
    enable = lib.mkEnableOption "periodic GATT queries of the connected Bluetooth device";

    interval = lib.mkOption {
      type = lib.types.str;
      default = "5min";
      description = "Query interval as a systemd time span.";
    };

    battery = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Read the device's battery level.";
    };

    cts = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Read the device's Current Time Service.";
    };
  };

  config = lib.mkIf (cfg.enable && query.enable) {
    systemd.services.bluetooth-auth-gatt-query = {
      description = "Read Bluetooth battery level and current time";
      requires = [ "bluetooth.service" ];
      after = [ "bluetooth.service" ];
      serviceConfig = {
        Type = "oneshot";
        ExecStart = utils.escapeSystemdExecArgs (
          [
            "${cfg.package}/bin/bluetooth-auth-gatt-query"
            "--address-file"
            cfg.device.address.file
          ]
          ++ lib.optional query.battery "--battery"
          ++ lib.optional query.cts "--cts"
        );
      };
    };

    systemd.timers.bluetooth-auth-gatt-query = {
      description = "Periodically query Bluetooth GATT values";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = query.interval;
        OnUnitActiveSec = query.interval;
      };
    };
  };
}
