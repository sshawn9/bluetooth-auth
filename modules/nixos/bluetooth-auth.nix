{
  bluetoothAuthPackage,
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.my.security.bluetoothAuth;
  jsonFormat = pkgs.formats.json { };
  defaultPolkitAllowedActions = [
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

    config = lib.mkOption {
      type = lib.types.submodule {
        freeformType = jsonFormat.type;
        options = {
          user = lib.mkOption {
            type = lib.types.str;
            default = "";
            example = "alice";
            description = "User trusted by sudo auth and targeted by auto-lock.";
          };

          bluetoothAddressFile = lib.mkOption {
            type = lib.types.str;
            default = "";
            example = lib.literalExpression "config.sops.secrets.auth_bluetooth_address.path";
            description = ''
              Runtime file containing the Bluetooth device address. Use this
              with secret managers such as sops-nix.
            '';
          };

          autoConnect = lib.mkOption {
            type = lib.types.submodule {
              freeformType = jsonFormat.type;
              options = {
                checkIntervalSeconds = lib.mkOption {
                  type = lib.types.ints.between 1 2147483647;
                  default = 30;
                  description = "How often to check whether the Bluetooth device is already connected.";
                };

                deviceUnvailableGraceSeconds = lib.mkOption {
                  type = lib.types.ints.between 1 2147483647;
                  default = 300;
                  description = "How long to wait when the adapter is off or the device is unavailable.";
                };

                exceptionGraceSeconds = lib.mkOption {
                  type = lib.types.ints.between 1 2147483647;
                  default = 300;
                  description = "How long to wait before retrying after a BlueZ or D-Bus error.";
                };
              };
            };
            default = { };
            description = "auto-connect JSON configuration.";
          };

          autoLock = lib.mkOption {
            type = lib.types.submodule {
              freeformType = jsonFormat.type;
              options = {
                checkIntervalSeconds = lib.mkOption {
                  type = lib.types.ints.between 1 2147483647;
                  default = 40;
                  description = "How often to query BlueZ Connected and check the session lock state.";
                };

                sleepAfterLockSeconds = lib.mkOption {
                  type = lib.types.ints.between 0 2147483647;
                  default = 3;
                  description = "How long to wait after successfully triggering the lock service.";
                };

                exceptionGraceSeconds = lib.mkOption {
                  type = lib.types.ints.between 1 2147483647;
                  default = 300;
                  description = "How long to wait before retrying after a BlueZ or D-Bus error.";
                };
              };
            };
            default = { };
            description = "auto-lock JSON configuration.";
          };

          sudoAuth = lib.mkOption {
            type = lib.types.submodule {
              freeformType = jsonFormat.type;
              options.timeoutSeconds = lib.mkOption {
                type = lib.types.ints.between 1 2147483647;
                default = 2;
                description = "Maximum time to wait for the Bluetooth connection check during sudo PAM auth.";
              };
            };
            default = { };
            description = "sudo auth JSON configuration.";
          };

          polkitAuth = lib.mkOption {
            type = lib.types.submodule {
              freeformType = jsonFormat.type;
              options = {
                timeoutSeconds = lib.mkOption {
                  type = lib.types.ints.between 1 2147483647;
                  default = 2;
                  description = "Maximum time to wait for the Bluetooth connection check during polkit auth.";
                };

                allowedActions = lib.mkOption {
                  type = lib.types.listOf lib.types.str;
                  default = defaultPolkitAllowedActions;
                  example = [ "org.freedesktop.systemd1.manage-units" ];
                  description = "polkit action ids that Bluetooth auth may authorize.";
                };
              };
            };
            default = { };
            description = "polkit auth JSON configuration.";
          };

          lockerAuth = lib.mkOption {
            type = lib.types.submodule {
              freeformType = jsonFormat.type;
              options.timeoutSeconds = lib.mkOption {
                type = lib.types.ints.between 1 2147483647;
                default = 2;
                description = "Maximum time to wait for the Bluetooth connection check during locker PAM auth.";
              };
            };
            default = { };
            description = "locker PAM auth JSON configuration.";
          };

          greetdAuth = lib.mkOption {
            type = lib.types.submodule {
              freeformType = jsonFormat.type;
              options.timeoutSeconds = lib.mkOption {
                type = lib.types.ints.between 1 2147483647;
                default = 2;
                description = "Maximum time to wait for the Bluetooth connection check during greetd PAM auth.";
              };
            };
            default = { };
            description = "greetd PAM auth JSON configuration.";
          };
        };
      };
      default = { };
      example = lib.literalExpression ''
        {
          user = "alice";
          bluetoothAddressFile = config.sops.secrets.auth_bluetooth_address.path;
          autoConnect.checkIntervalSeconds = 30;
          autoConnect.deviceUnvailableGraceSeconds = 300;
          autoConnect.exceptionGraceSeconds = 300;
          autoLock.checkIntervalSeconds = 40;
          autoLock.sleepAfterLockSeconds = 3;
          autoLock.exceptionGraceSeconds = 300;
          sudoAuth.timeoutSeconds = 2;
          polkitAuth.timeoutSeconds = 2;
          lockerAuth.timeoutSeconds = 2;
          greetdAuth.timeoutSeconds = 2;
        }
      '';
      description = ''
        Attribute set converted directly to the JSON configuration file passed
        to bluetooth-auth commands. Use bluetoothAddressFile with secret
        managers such as sops-nix.
      '';
    };
  };
}
