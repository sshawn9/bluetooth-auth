cfg:

let
  settings = {
    inherit (cfg) user bluetoothAddressFile;

    autoConnect = {
      inherit (cfg.autoConnect)
        checkIntervalSeconds
        deviceUnvailableGraceSeconds
        exceptionGraceSeconds
        reconnectTimes
        reconnectIntervalSeconds
        ;
    };

    autoLock = {
      inherit (cfg.autoLock)
        checkIntervalSeconds
        sleepAfterLockSeconds
        exceptionGraceSeconds
        ;
    };

    sudoAuth = {
      inherit (cfg.sudoAuth) timeoutSeconds;
    };

    polkitAuth = {
      inherit (cfg.polkitAuth) timeoutSeconds allowedActions;
    };

    lockerAuth = {
      inherit (cfg.lockerAuth) timeoutSeconds;
    };

    greetdAuth = {
      inherit (cfg.greetdAuth) timeoutSeconds;
    };
  };
in
builtins.toFile "bluetooth-auth-settings.json" (builtins.toJSON settings)
