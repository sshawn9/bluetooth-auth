use std::{error::Error, fs, path::PathBuf, process::ExitCode};

use bluer::{Address, Uuid};
use clap::Parser;
use serde_json::{Map, Value, json};

#[derive(Parser)]
#[command(
    about = "Read the target's battery level and current time once over GATT",
    after_help = "Requires an existing connection on hci0. Does not connect or subscribe.\n\
                  Reads remote characteristics rather than cached battery properties.\n\
                  CTS contains the device's local time, without a time zone."
)]
struct Args {
    /// File containing the target's Bluetooth identity address
    #[arg(long, value_name = "PATH")]
    address_file: PathBuf,

    /// Read battery level
    #[arg(long)]
    battery: bool,

    /// Read current time
    #[arg(long)]
    cts: bool,
}

#[tokio::main(flavor = "current_thread")]
async fn main() -> ExitCode {
    match run().await {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("GATT query failed: {error}");
            ExitCode::FAILURE
        }
    }
}

async fn run() -> Result<(), Box<dyn Error>> {
    let args = Args::parse();
    let target: Address = fs::read_to_string(args.address_file)?.trim().parse()?;
    let adapter = bluer::Session::new().await?.adapter("hci0")?;
    let mut device = None;
    // A device discovered before pairing can retain a temporary-address path.
    for address in adapter.device_addresses().await? {
        let candidate = adapter.device(address)?;
        if candidate.remote_address().await? == target {
            device = Some(candidate);
            break;
        }
    }
    let device = device.ok_or("Target device not found")?;
    if !device.is_connected().await? {
        return Err("Target device is not connected".into());
    }
    let services = device.services().await?;
    let uuid_base = 0x00000000_0000_1000_8000_00805f9b34fb_u128;
    let mut result = Map::new();
    for (name, service_id, characteristic_id, selected) in [
        ("battery", 0x180f_u16, 0x2a19_u16, args.battery),
        ("cts", 0x1805, 0x2a2b, args.cts),
    ] {
        if !selected {
            continue;
        }
        let mut characteristic = None;
        for service in &services {
            if service.uuid().await? != Uuid::from_u128(uuid_base | (u128::from(service_id) << 96))
            {
                continue;
            }
            for candidate in service.characteristics().await? {
                if candidate.uuid().await?
                    == Uuid::from_u128(uuid_base | (u128::from(characteristic_id) << 96))
                {
                    characteristic = Some(candidate);
                    break;
                }
            }
            if characteristic.is_some() {
                break;
            }
        }
        let value = characteristic
            .ok_or_else(|| format!("{name} characteristic not found"))?
            .read()
            .await?;
        match (name, value.as_slice()) {
            ("battery", [percentage]) if *percentage <= 100 => {
                result.insert("battery_percent".into(), json!(percentage));
            }
            (
                "cts",
                [
                    year_lo,
                    year_hi,
                    month,
                    day,
                    hours,
                    minutes,
                    seconds,
                    day_of_week,
                    fractions256,
                    adjust_reason,
                ],
            ) => {
                result.insert(
                    "cts".into(),
                    json!({
                        "year": u16::from_le_bytes([*year_lo, *year_hi]),
                        "month": month,
                        "day": day,
                        "hours": hours,
                        "minutes": minutes,
                        "seconds": seconds,
                        "day_of_week": day_of_week,
                        "fractions256": fractions256,
                        "adjust_reason": adjust_reason,
                    }),
                );
            }
            _ => return Err(format!("Invalid {name} characteristic value").into()),
        }
    }
    println!("{}", Value::Object(result));
    Ok(())
}
