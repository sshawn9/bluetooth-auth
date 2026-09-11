use std::{error::Error, fs, path::PathBuf, process::ExitCode, time::Duration};

use bluetooth_auth::{advertise, query_connection, register_hid};
use clap::Parser;
use tokio::{runtime::Builder, time};

#[derive(Parser)]
#[command(
    name = "ble-ask-or-connect",
    about = "Check the target's encrypted LE connection, or offer HID once to connect",
    after_help = "Requires an existing HID/LE pairing on hci0.\n\
                  Temporary HID and advertising are released on exit; no disconnect is sent.\n\
                  Exit status: 0 = ready, 1 = operation failed, 2 = invalid arguments."
)]
struct Args {
    /// File containing the target's Bluetooth identity address
    #[arg(long, value_name = "PATH")]
    address_file: PathBuf,

    /// Total timeout for HID registration, advertising and waiting
    #[arg(long, value_name = "SECONDS", default_value_t = 15,
          value_parser = clap::value_parser!(u32).range(1..))]
    timeout_seconds: u32,
}

fn main() -> ExitCode {
    match ask_or_connect() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("BLE operation failed: {error}");
            ExitCode::FAILURE
        }
    }
}

fn ask_or_connect() -> Result<(), Box<dyn Error>> {
    let Args {
        address_file,
        timeout_seconds,
    } = Args::parse();
    let target = fs::read_to_string(address_file)?.trim().parse()?;

    if query_connection(target)? {
        return Ok(());
    }

    Builder::new_current_thread()
        .enable_all()
        .build()?
        .block_on(async {
            time::timeout(Duration::from_secs(u64::from(timeout_seconds)), async {
                let _hid = register_hid(Some(target)).await?;
                let _advertisement = advertise().await?;
                // Observe this attempt; never restart HID registration or advertising.
                while !query_connection(target)? {
                    time::sleep(Duration::from_millis(100)).await;
                }
                Ok::<(), bluer::Error>(())
            })
            .await
        })
        .map_err(|_| format!("Connection attempt timed out after {timeout_seconds} seconds"))??;
    // Runtime shutdown closes the D-Bus connections, including partial registrations.

    Ok(())
}
