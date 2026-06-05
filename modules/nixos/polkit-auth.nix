{
  config,
  lib,
  ...
}:

let
  cfg = config.my.security.bluetoothAuth;
  mkSettingsFile = import ./settings-file.nix;
  settingsFile = mkSettingsFile cfg;
  helper = "${cfg.package}/bin/bluetooth-auth-oneshot-auth";
  allowedActions = builtins.toJSON cfg.polkitAuth.allowedActions;
  trustedUser = builtins.toJSON cfg.user;
  defaultAllowedActions = [
    "org.freedesktop.login1.power-off"
    "org.freedesktop.login1.power-off-multiple-sessions"
    "org.freedesktop.login1.reboot"
    "org.freedesktop.login1.reboot-multiple-sessions"
    "org.freedesktop.login1.suspend"
    "org.freedesktop.login1.suspend-multiple-sessions"
    "org.freedesktop.login1.hibernate"
    "org.freedesktop.login1.hibernate-multiple-sessions"
    "org.freedesktop.login1.lock-sessions"

    "org.freedesktop.systemd1.manage-units"
    "org.freedesktop.systemd1.reload-daemon"

    "org.freedesktop.NetworkManager.enable-disable-network"
    "org.freedesktop.NetworkManager.enable-disable-wifi"
    "org.freedesktop.NetworkManager.network-control"
    "org.freedesktop.NetworkManager.settings.modify.own"
    "org.freedesktop.NetworkManager.settings.modify.system"
    "org.freedesktop.NetworkManager.wifi.scan"

    "org.freedesktop.udisks2.filesystem-mount"
    "org.freedesktop.udisks2.filesystem-mount-system"
    "org.freedesktop.udisks2.filesystem-unmount-others"
    "org.freedesktop.udisks2.encrypted-unlock"
    "org.freedesktop.udisks2.encrypted-unlock-system"
    "org.freedesktop.udisks2.eject-media"
    "org.freedesktop.udisks2.power-off-drive"

    "org.freedesktop.UPower.PowerProfiles.switch-profile"
    "org.freedesktop.UPower.enable-charging-limit"
  ];
in
{
  options.my.security.bluetoothAuth.polkitAuth = {
    enable = lib.mkEnableOption "polkit authentication bypass when the Bluetooth device is connected";

    timeoutSeconds = lib.mkOption {
      type = lib.types.ints.between 1 2147483647;
      default = 2;
      description = "Maximum time to wait for the Bluetooth connection check during polkit auth.";
    };

    allowedActions = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = defaultAllowedActions;
      example = [ "org.freedesktop.systemd1.manage-units" ];
      description = "polkit action ids that Bluetooth auth may authorize.";
    };
  };

  config = lib.mkIf (cfg.enable && cfg.polkitAuth.enable) {
    security.polkit.enable = true;

    security.polkit.extraConfig = ''
      polkit.addRule(function(action, subject) {
        var allowedActions = ${allowedActions};

        if (
          !subject.user ||
          subject.user != ${trustedUser} ||
          !subject.local ||
          !subject.active
        ) {
          return polkit.Result.NOT_HANDLED;
        }

        if (allowedActions.indexOf(action.id) == -1) {
          return polkit.Result.NOT_HANDLED;
        }

        try {
          polkit.spawn([
            ${builtins.toJSON helper},
            ${builtins.toJSON "${settingsFile}"},
            "polkit"
          ]);
          return polkit.Result.YES;
        } catch (error) {
          return polkit.Result.NOT_HANDLED;
        }
      });
    '';
  };
}
