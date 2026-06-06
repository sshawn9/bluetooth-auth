# Auto-Connect Event-Driven Refactor Plan

This note records a future refactor plan for `bluetooth-auth-auto-connect`.
The current implementation is timer-driven: it wakes periodically, checks the
BlueZ state, and calls `Device1.Connect()` when the configured device is
paired, trusted, and disconnected.

The desired direction is to make auto-connect event-driven first, with periodic
polling kept only as a fallback.

## Goal

Move from:

```text
timer wakes up -> check BlueZ state -> maybe connect
```

to:

```text
login1 or BlueZ event wakes up -> check BlueZ state -> maybe connect
timer wakes up occasionally -> fallback check
```

The auto-connect service should react quickly after boot, after resume from
suspend, after BlueZ restarts, and after the Bluetooth adapter or target device
becomes available.

## Event Sources

Only two event domains should drive immediate reconnect checks:

- login1 lifecycle events;
- BlueZ lifecycle and state events.

Other signals are likely to be indirect or noisy and should not become primary
auto-connect triggers.

## login1 Events

Listen to:

```text
org.freedesktop.login1.Manager.PrepareForSleep(false)
```

`false` means the system has resumed from sleep. When this is received,
auto-connect should wake immediately and re-read the current BlueZ state.

The handler should not call `Device1.Connect()` directly. It should only wake
the main loop.

## BlueZ Events

### BlueZ Service Lifecycle

Listen to system bus name owner changes for:

```text
org.bluez
```

Relevant event:

```text
org.freedesktop.DBus.NameOwnerChanged
```

When `org.bluez` appears or its owner changes, close the existing BlueZ proxy
state and wake the main loop. The next loop iteration should reinitialize
BlueZ objects before checking device state.

### ObjectManager Changes

Listen to:

```text
org.freedesktop.DBus.ObjectManager.InterfacesAdded
org.freedesktop.DBus.ObjectManager.InterfacesRemoved
```

Only react when the event is relevant to the configured adapter or target
device:

```text
/org/bluez/hci0
/org/bluez/hci0/dev_XX_XX_XX_XX_XX_XX
```

If matching by path is not enough, match `org.bluez.Device1.Address` against
the configured Bluetooth address.

When the target device appears, wake the main loop. When it disappears, close
the stale device proxy state and let the main loop wait for a later event or
timer fallback.

### Adapter Properties

Listen to `PropertiesChanged` on the adapter:

```text
interface = org.bluez.Adapter1
property = Powered
```

Only `Powered=true` needs to wake auto-connect immediately. `Powered=false`
can update internal state or close stale proxies, but it should not trigger a
connect attempt.

### Device Properties

Listen to `PropertiesChanged` on the target device:

```text
interface = org.bluez.Device1
properties = Connected, Paired, Trusted
```

Wake the main loop when any of these changes:

- `Connected=false`: the device may need reconnecting;
- `Paired=true`: the device may have become connectable;
- `Trusted=true`: the device may have become eligible for auto-connect;
- `Connected=true`: no connect is needed, but waking lets the loop observe and
  settle the latest state.

## Non-Goals

Do not use these as primary auto-connect triggers:

- PipeWire or WirePlumber service state;
- systemd unit state for unrelated services;
- desktop session active/unlocked state;
- RSSI changes;
- device `Name`, `Alias`, `Icon`, or `UUIDs`;
- `ServicesResolved`;
- PipeWire socket activity.

These signals are either indirect, noisy, or not reliable readiness indicators
for `Device1.Connect()`.

## Main Loop Model

All event handlers should do one thing:

```text
kick_event.set()
```

They should not call `Device1.Connect()` directly.

The main loop should remain the only place that performs connection decisions:

```text
wait for kick event or timeout
clear/coalesce pending kicks
initialize BlueZ proxies if needed
read Powered, Paired, Trusted, Connected
if Powered && Paired && Trusted && !Connected:
    run reconnect burst
else:
    wait again
```

This keeps connection attempts serialized and prevents a burst of D-Bus
signals from causing concurrent `Device1.Connect()` calls.

## Timer Fallback

Keep `autoConnect.checkIntervalSeconds` as a fallback timer.

Its role changes from primary driver to safety net:

- recover if a D-Bus signal is missed;
- recover if BlueZ emits fewer events than expected;
- recover after unexpected internal state drift;
- keep the daemon simple to reason about under imperfect event delivery.

The timer should be interruptible by the kick event.

## Reconnect Burst

The existing reconnect settings should continue to control active connection
attempts:

```text
autoConnect.reconnectTimes
autoConnect.reconnectIntervalSeconds
```

When the main loop decides the device is eligible and disconnected, it should
run the existing reconnect burst. D-Bus errors can retry within the burst.
Transport or proxy-level `OSError` should close the device proxy state and let
the next loop iteration reinitialize BlueZ.

## Implementation Sketch

1. Add a shared `asyncio.Event` used as the auto-connect kick signal.
2. Replace direct `asyncio.sleep(...)` waits with an interruptible wait that
   returns when either the timeout expires or the kick event is set.
3. Add `SIGUSR1` handling as a simple manual/systemd kick path.
4. Add login1 `PrepareForSleep(false)` monitoring.
5. Add BlueZ lifecycle monitoring through `NameOwnerChanged`.
6. Add BlueZ ObjectManager and `PropertiesChanged` monitoring, filtered to the
   configured adapter and target device.
7. Keep periodic polling as a fallback.

## NixOS Integration

Once `SIGUSR1` kick support exists, NixOS can optionally provide an explicit
resume hook:

```nix
powerManagement.resumeCommands = ''
  systemctl kill -s SIGUSR1 bluetooth-auth-auto-connect.service || true
'';
```

If login1 monitoring is implemented inside the daemon, this hook may be
unnecessary, but it remains useful as a simple fallback or debugging tool.

## Success Criteria

- The service attempts connection immediately on startup.
- After resume from suspend, the service wakes immediately instead of waiting
  for a long unavailable/error sleep to expire.
- After BlueZ restarts, the service discards stale proxies and reinitializes.
- When the adapter becomes powered, the service checks the target device
  immediately.
- When the target device appears or changes `Connected`, `Paired`, or
  `Trusted`, the service checks immediately.
- Connection attempts remain serialized through the main loop.
- Timer-based polling still works as a fallback.

## Testing Notes

Useful manual checks:

```sh
systemctl kill -s SIGUSR1 bluetooth-auth-auto-connect.service
journalctl -u bluetooth-auth-auto-connect.service -f
```

Suspend/resume:

```sh
systemctl suspend
journalctl -b -u bluetooth-auth-auto-connect.service --no-pager -o short-precise
```

BlueZ restart:

```sh
systemctl restart bluetooth.service
journalctl -b -u bluetooth-auth-auto-connect.service -u bluetooth.service --no-pager -o short-precise
```

Adapter/device state:

```sh
bluetoothctl show
bluetoothctl info AA:BB:CC:DD:EE:FF
```
