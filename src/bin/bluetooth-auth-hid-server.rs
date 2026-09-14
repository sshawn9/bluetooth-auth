use std::{error::Error, fs::File, os::unix::fs::OpenOptionsExt, time::Duration};

use bluetooth_auth::{LOCK_PATH, advertise, register_hid};
use dbus::blocking::{Connection, stdintf::org_freedesktop_dbus::Properties};
use tokio::{
    runtime::Builder,
    signal::unix::{SignalKind, signal},
};

fn main() -> Result<(), Box<dyn Error>> {
    let lock = File::options()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(LOCK_PATH)
        .map_err(|error| format!("Cannot open shared connection lock {LOCK_PATH}: {error}"))?;
    eprintln!("Waiting for exclusive use of the HID connection helper");
    lock.lock()?;
    let bus = Connection::new_system()?;
    let adapter = bus.with_proxy("org.bluez", "/org/bluez/hci0", Duration::from_secs(5));
    let interface = "org.bluez.Adapter1";
    let mut original = Vec::new();
    for property in ["Pairable", "Discoverable", "Connectable"] {
        original.push((property, adapter.get::<bool>(interface, property)?));
    }
    let pairable_timeout: u32 = adapter.get(interface, "PairableTimeout")?;

    let runtime = Builder::new_current_thread().enable_all().build()?;
    let result: Result<(), Box<dyn Error>> = runtime.block_on(async {
        let mut interrupt = signal(SignalKind::interrupt())?;
        let mut terminate = signal(SignalKind::terminate())?;
        tokio::select! {
            result = async {
                adapter.set(interface, "Discoverable", false)?;
                adapter.set(interface, "PairableTimeout", 0_u32)?;
                adapter.set(interface, "Pairable", true)?;
                eprintln!("Classic Bluetooth discovery is temporarily disabled; LE pairing is enabled.");
                eprintln!("Keep your computer's Bluetooth pairing tool open for confirmation.");
                let _hid = register_hid(None).await?;
                let _advertisement = advertise().await?;
                eprintln!("HID advertising started. Select this computer on your iPhone and confirm pairing.");
                eprintln!("Press Ctrl+C to stop and restore adapter settings, keeping the pairing.");
                std::future::pending().await
            } => result,
            _ = interrupt.recv() => Ok(()),
            _ = terminate.recv() => Ok(()),
        }
    });
    // Close even partial HID/advertisement registrations before restoring the
    // adapter. Keep the shared lock until restoration has also finished.
    drop(runtime);

    // Attempt every restoration even if registration or another restoration failed.
    // Discoverable may also change Connectable, so restore Connectable afterwards.
    let mut errors = Vec::new();
    for (property, value) in original {
        if let Err(error) = adapter.set(interface, property, value) {
            errors.push(format!("Cannot restore {property}: {error}"));
        }
    }
    if let Err(error) = adapter.set(interface, "PairableTimeout", pairable_timeout) {
        errors.push(format!("Cannot restore PairableTimeout: {error}"));
    }
    if errors.is_empty() {
        eprintln!("Original adapter settings restored; pairing records retained.");
    }
    if let Err(error) = result {
        errors.insert(0, error.to_string());
    }
    if errors.is_empty() {
        Ok(())
    } else {
        Err(errors.join("; ").into())
    }
}
