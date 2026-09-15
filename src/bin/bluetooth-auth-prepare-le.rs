//! Internal capability helper; the target identity address is supplied on stdin.

use std::{
    error::Error,
    io::{self, Read},
    process::ExitCode,
};

use bluetooth_auth::prepare_le;

fn main() -> ExitCode {
    match run() {
        Ok(true) => ExitCode::SUCCESS,
        Ok(false) => ExitCode::FAILURE,
        Err(error) => {
            eprintln!("Cannot read LE preparation target: {error}");
            ExitCode::FAILURE
        }
    }
}

fn run() -> Result<bool, Box<dyn Error>> {
    let target = io::read_to_string(io::stdin().take(18))?.trim().parse()?;
    Ok(prepare_le(target))
}
