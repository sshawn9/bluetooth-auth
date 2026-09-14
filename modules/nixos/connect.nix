{
  config,
  lib,
  utils,
  ...
}:

let
  cfg = config.my.security.bluetoothAuth;
  timeout = cfg.connect.timeoutMilliseconds;
in
{
  options.my.security.bluetoothAuth.connect.timeoutMilliseconds = lib.mkOption {
    type = lib.types.ints.between 1 2147483647;
    default = 7000;
    example = 5000;
    description = "Maximum duration of one background HID connection attempt, in milliseconds.";
  };

  config =
    lib.mkIf
      (
        cfg.enable
        && (
          cfg.sudoAuth.enable
          || cfg.lockerAuth.enable
          || cfg.polkitAuth.enable
          || cfg.greetdAuth.enable
          || cfg.noctaliaAutoLock.enable
        )
      )
      {
        systemd.tmpfiles.rules = [
          "d /run/bluetooth-auth 0750 root ${cfg.group} -"
          "f /run/bluetooth-auth/hci0.lock 0660 root ${cfg.group} -"
        ];

        systemd.sockets.bluetooth-auth-connect = {
          description = "Request one Bluetooth HID connection attempt";
          wantedBy = [ "sockets.target" ];
          after = [ "systemd-tmpfiles-setup.service" ];
          socketConfig = {
            ListenDatagram = "/run/bluetooth-auth/connect.sock";
            SocketUser = "root";
            SocketGroup = cfg.group;
            SocketMode = "0660";
            DirectoryMode = "0750";
            Accept = false;
            FlushPending = true;
            RemoveOnStop = true;
            # A later authentication request must still work after repeated fast failures.
            TriggerLimitIntervalSec = 0;
          };
        };

        systemd.services.bluetooth-auth-connect = {
          description = "Attempt one Bluetooth HID connection";
          requires = [ "bluetooth.service" ];
          after = [
            "bluetooth.service"
            "systemd-tmpfiles-setup.service"
          ];
          startLimitIntervalSec = 0;
          serviceConfig = {
            Type = "oneshot";
            ExecStart = utils.escapeSystemdExecArgs [
              "${cfg.package}/bin/ble-link"
              "--address-file"
              cfg.bluetoothAddressFile
              "--connect"
              (toString timeout)
            ];
            Restart = "no";
            RemainAfterExit = false;
            # Allow normal cleanup after the program's connection budget expires.
            TimeoutStartSec = "${toString (timeout + 1000)}ms";
            TimeoutStopSec = "1s";
          };
        };
      };
}
