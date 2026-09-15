//! Hold the project's HID application without changing adapter pairing settings.
use std::{error::Error, fs, path::PathBuf, process::ExitCode};

use bluetooth_auth::{LOCK_PATH, advertise, register_hid};
use clap::Parser;
use tokio::{
    runtime::Builder,
    signal::unix::{SignalKind, signal},
};

#[derive(Parser)]
#[command(about = "Hold target-restricted HID and advertising on hci0 until Ctrl-C")]
struct Args {
    #[arg(long)]
    address_file: PathBuf,
}

fn run(args: Args) -> Result<(), Box<dyn Error>> {
    let target = fs::read_to_string(args.address_file)?.trim().parse()?;
    let lock = fs::File::open(LOCK_PATH)?;
    lock.try_lock()?;
    let runtime = Builder::new_current_thread().enable_all().build()?;
    runtime.block_on(async {
        let mut stop = signal(SignalKind::terminate())?;
        let mut interrupt = signal(SignalKind::interrupt())?;
        let _hid = register_hid(Some(target)).await?;
        let _advertisement = advertise().await?;
        println!("HID_READY");
        tokio::select! {
            _ = stop.recv() => (),
            _ = interrupt.recv() => (),
        }
        Ok::<_, Box<dyn Error>>(())
    })?;
    drop(runtime);
    println!("HID_CLOSED");
    Ok(())
}

fn main() -> ExitCode {
    match run(Args::parse()) {
        Ok(()) => ExitCode::SUCCESS,
        Err(_) => {
            // Library errors may contain the peer address or its D-Bus path.
            eprintln!(
                "HID holder failed; check the address file, connection lock and local BlueZ journal"
            );
            ExitCode::FAILURE
        }
    }
}
