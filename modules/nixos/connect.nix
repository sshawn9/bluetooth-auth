{
  config,
  lib,
  utils,
  ...
}:

let
  cfg = config.my.security.bluetoothAuth;
  timeout = cfg.connection.timeoutMs;
  socketEnabled =
    cfg.enable
    && (
      cfg.auth.sudo.enable
      || cfg.auth.locker.enable
      || cfg.auth.polkit.enable
      || cfg.auth.greetd.enable
      || cfg.noctaliaAutoLock.enable
      || cfg.gnomeKeyringUnlock.enable
    );
  autoConnectEnabled = cfg.enable && cfg.autoConnect.enable;
in
{
  options.my.security.bluetoothAuth.connection.timeoutMs = lib.mkOption {
    type = lib.types.ints.between 1 2147483647;
    default = 7000;
    example = 5000;
    description = "Maximum duration of one HID connection attempt and LE preparation, in milliseconds, shared by authentication helpers and background services.";
  };

  config = lib.mkIf (socketEnabled || autoConnectEnabled) {
    security.wrappers.bluetooth-auth-prepare-le = {
      source = "${cfg.package}/bin/bluetooth-auth-prepare-le";
      owner = "root";
      group = cfg.accessGroup;
      permissions = "u+rx,g+x,o-rwx";
      capabilities = "cap_net_admin+ep";
    };

    systemd.tmpfiles.rules = [
      "d /run/bluetooth-auth 0750 root ${cfg.accessGroup} -"
      "f /run/bluetooth-auth/hci0.lock 0660 root ${cfg.accessGroup} -"
    ];

    systemd.sockets.bluetooth-auth-connect = lib.mkIf socketEnabled {
      description = "Request one Bluetooth HID connection attempt";
      wantedBy = [ "sockets.target" ];
      after = [ "systemd-tmpfiles-setup.service" ];
      socketConfig = {
        ListenDatagram = "/run/bluetooth-auth/connect.sock";
        SocketUser = "root";
        SocketGroup = cfg.accessGroup;
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
          "${cfg.package}/bin/bluetooth-auth-link"
          "--address-file"
          cfg.device.address.file
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
