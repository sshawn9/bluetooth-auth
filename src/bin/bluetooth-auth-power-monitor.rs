use std::{error::Error, fs, path::PathBuf, process::ExitCode};

use bluetooth_auth::query_or_connect;
use clap::Parser;
use dbus::{
    MessageType,
    arg::{PropMap, prop_cast},
    blocking::Connection,
};

const ADAPTER: &str = "/org/bluez/hci0";
const ADAPTER_INTERFACE: &str = "org.bluez.Adapter1";

#[derive(Parser)]
#[command(about = "Attempt a BLE connection when hci0 is powered on")]
struct Args {
    /// File containing the target's Bluetooth identity address
    #[arg(long, value_name = "PATH")]
    address_file: PathBuf,

    /// Maximum duration of one BLE connection attempt, in milliseconds
    #[arg(long, default_value_t = 7_000)]
    timeout_ms: u64,
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("Bluetooth power monitoring failed: {error}");
            ExitCode::FAILURE
        }
    }
}

fn run() -> Result<(), Box<dyn Error>> {
    let args = Args::parse();
    let target = fs::read_to_string(args.address_file)?.trim().parse()?;
    let connection = Connection::new_system()?;
    connection.add_match_no_cb(
        "type='signal',sender='org.bluez',path='/org/bluez/hci0',interface='org.freedesktop.DBus.Properties',member='PropertiesChanged',arg0='org.bluez.Adapter1'",
    )?;
    connection.request_name("org.bluetooth_auth.PowerMonitor", false, false, true)?;

    loop {
        let Some(message) = connection.channel().pop_message() else {
            connection
                .channel()
                .read_write(None)
                .map_err(|_| "System D-Bus disconnected")?;
            continue;
        };
        if message.msg_type() != MessageType::Signal
            || message.destination().is_some()
            || message.path().as_deref() != Some(ADAPTER)
            || message.interface().as_deref() != Some("org.freedesktop.DBus.Properties")
            || message.member().as_deref() != Some("PropertiesChanged")
        {
            continue;
        }

        let Ok((interface, changed, _)): Result<(String, PropMap, Vec<String>), _> =
            message.read3()
        else {
            continue;
        };
        if interface != ADAPTER_INTERFACE
            || prop_cast::<bool>(&changed, "Powered").copied() != Some(true)
        {
            continue;
        }

        if let Err(error) = query_or_connect(target, args.timeout_ms) {
            eprintln!("BLE connection attempt failed: {error}");
        }
    }
}
