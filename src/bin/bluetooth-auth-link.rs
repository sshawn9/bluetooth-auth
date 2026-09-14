use std::{error::Error, fs, os::unix::net::UnixDatagram, path::PathBuf, process::ExitCode};

use bluetooth_auth::{query_connection, query_or_connect};
use clap::Parser;

const SOCKET_PATH: &str = "/run/bluetooth-auth/connect.sock";

#[derive(Parser)]
#[command(
    name = "bluetooth-auth-link",
    about = "Check the target's encrypted LE connection and optionally connect once",
    after_help = "Requires an existing HID/LE pairing on hci0.\n\
                  Synchronous attempts require the pre-created lock /run/bluetooth-auth/hci0.lock.\n\
                  Waiting for another attempt and connecting share the synchronous timeout.\n\
                  Asynchronous requests require /run/bluetooth-auth/connect.sock.\n\
                  Temporary HID and advertising are released on exit; no disconnect is sent.\n\
                  Exit status: 0 = ready, 1 = not ready or operation failed, 2 = invalid arguments."
)]
struct Args {
    /// File containing the target's Bluetooth identity address
    #[arg(long, value_name = "PATH")]
    address_file: PathBuf,

    /// 0: query only; positive: synchronous timeout in ms; -1: request a background attempt
    #[arg(
        long,
        value_name = "MILLISECONDS",
        default_value_t = 15_000,
        allow_negative_numbers = true
    )]
    connect: i64,
}

fn main() -> ExitCode {
    match run() {
        Ok(true) => ExitCode::SUCCESS,
        Ok(false) => ExitCode::FAILURE,
        Err(error) => {
            eprintln!("BLE operation failed: {error}");
            ExitCode::FAILURE
        }
    }
}

fn run() -> Result<bool, Box<dyn Error>> {
    let args = Args::parse();
    let target = fs::read_to_string(args.address_file)?.trim().parse()?;
    if query_connection(target)? {
        return Ok(true);
    }
    if args.connect > 0 {
        return query_or_connect(target, args.connect as u64);
    }
    if args.connect < 0 {
        let socket = UnixDatagram::unbound()?;
        socket.set_nonblocking(true)?;
        socket.send_to(&[1], SOCKET_PATH)?;
    }
    Ok(false)
}
