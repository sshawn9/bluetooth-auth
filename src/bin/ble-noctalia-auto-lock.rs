use std::{
    error::Error,
    fs,
    path::PathBuf,
    process::{Command, ExitCode},
    thread,
    time::Duration,
};

use bluetooth_auth::query_or_connect;
use clap::Parser;

#[derive(Parser)]
#[command(
    about = "Monitor BLE and lock with Noctalia v5, sleeping between checks based on session state",
    after_help = "Requires noctalia in PATH and an existing HID/LE pairing on hci0.\n\
                  Use the running Noctalia session's XDG_RUNTIME_DIR and WAYLAND_DISPLAY.\n\
                  Requires Bluetooth access and the pre-created lock /run/bluetooth-auth/hci0.lock."
)]
struct Args {
    /// File containing the target's Bluetooth identity address
    #[arg(long, value_name = "PATH")]
    address_file: PathBuf,

    /// Maximum duration of the single BLE connection attempt, in milliseconds
    #[arg(long, default_value_t = 7_000)]
    timeout_ms: u64,

    /// Sleep after processing while the session is unlocked and BLE connected, in milliseconds
    #[arg(long, default_value_t = 30_000, value_parser = clap::value_parser!(u64).range(1..))]
    unlocked_connected_interval_ms: u64,

    /// Sleep between checks while unlocked without an encrypted BLE connection, in milliseconds
    #[arg(long, default_value_t = 30_000, value_parser = clap::value_parser!(u64).range(1..))]
    unlocked_disconnected_interval_ms: u64,

    /// Sleep between checks while locked with an encrypted BLE connection, in milliseconds
    #[arg(long, default_value_t = 120_000, value_parser = clap::value_parser!(u64).range(1..))]
    locked_connected_interval_ms: u64,

    /// Sleep between checks while locked without an encrypted BLE connection, in milliseconds
    #[arg(long, default_value_t = 60_000, value_parser = clap::value_parser!(u64).range(1..))]
    locked_disconnected_interval_ms: u64,
}

fn main() -> ExitCode {
    match run_loop() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("Automatic locking failed: {error}");
            ExitCode::FAILURE
        }
    }
}

fn run_loop() -> Result<(), Box<dyn Error>> {
    let args = Args::parse();
    let target = fs::read_to_string(args.address_file)?.trim().parse()?;

    loop {
        let (locked, connected) = run(target, args.timeout_ms)?;
        let interval_ms = match (locked, connected) {
            (false, true) => args.unlocked_connected_interval_ms,
            (false, false) => args.unlocked_disconnected_interval_ms,
            (true, true) => args.locked_connected_interval_ms,
            (true, false) => args.locked_disconnected_interval_ms,
        };
        thread::sleep(Duration::from_millis(interval_ms));
    }
}

fn run(target: bluer::Address, timeout_ms: u64) -> Result<(bool, bool), Box<dyn Error>> {
    let locked = query_locked()?;
    let connected = query_or_connect(target, timeout_ms).unwrap_or_else(|error| {
        eprintln!("Failed to check or establish the BLE connection: {error}");
        false
    });

    if locked || connected {
        return Ok((locked, connected));
    }

    noctalia(&["session", "lock"])?;
    thread::sleep(Duration::from_millis(300));
    let locked = query_locked()?;
    if !locked {
        eprintln!(
            "Session locking failed: Noctalia still reports an unlocked session after 300 ms"
        );
    }

    Ok((locked, connected))
}

fn query_locked() -> Result<bool, Box<dyn Error>> {
    let state: serde_json::Value = serde_json::from_slice(&noctalia(&["status"])?)?;
    // Noctalia reports true both while locking and after the session is locked.
    state["locked"]
        .as_bool()
        .ok_or_else(|| "Noctalia status does not contain a boolean locked field".into())
}

fn noctalia(args: &[&str]) -> Result<Vec<u8>, Box<dyn Error>> {
    let output = Command::new("noctalia").arg("msg").args(args).output()?;
    if !output.status.success() {
        return Err(format!(
            "noctalia msg {} failed ({}): {}{}",
            args.join(" "),
            output.status,
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr),
        )
        .into());
    }
    Ok(output.stdout)
}
