use std::{
    error::Error,
    fs,
    path::PathBuf,
    process::{Command, ExitCode},
    time::Duration,
};

use bluetooth_auth::query_or_connect;
use clap::Parser;
use dbus::{
    Path,
    arg::Variant,
    blocking::{Connection, stdintf::org_freedesktop_dbus::Properties},
};

#[derive(Parser)]
#[command(
    about = "Unlock the GNOME login keyring with SOPS, optionally requiring a Bluetooth connection"
)]
struct Args {
    /// Require this target's encrypted LE connection; omit to skip Bluetooth
    #[arg(long, value_name = "PATH")]
    address_file: Option<PathBuf>,

    /// Maximum connection wait in milliseconds when an address file is provided
    #[arg(long, default_value_t = 7000)]
    timeout_ms: u64,

    /// SOPS-encrypted file containing the existing login keyring password
    #[arg(long, value_name = "PATH")]
    sops_file: PathBuf,

    /// Top-level string key containing the password in the encrypted file
    #[arg(long, default_value = "login_keyring_password")]
    sops_key: String,
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("Failed to unlock the login keyring: {error}");
            ExitCode::FAILURE
        }
    }
}

fn run() -> Result<(), Box<dyn Error>> {
    let args = Args::parse();
    let connection = Connection::new_session()?;
    let service = connection.with_proxy(
        "org.freedesktop.secrets",
        "/org/freedesktop/secrets",
        Duration::from_secs(5),
    );
    let (collection,): (Path<'static>,) =
        service.method_call("org.freedesktop.Secret.Service", "ReadAlias", ("login",))?;
    let keyring = connection.with_proxy(
        "org.freedesktop.secrets",
        collection.clone(),
        Duration::from_secs(5),
    );
    if !keyring.get::<bool>("org.freedesktop.Secret.Collection", "Locked")? {
        return Ok(());
    }

    if let Some(address_file) = args.address_file {
        let target = fs::read_to_string(address_file)?.trim().parse()?;
        if !query_or_connect(target, args.timeout_ms)? {
            return Ok(());
        }
    }

    let output = Command::new("sops")
        .arg("decrypt")
        .arg("--extract")
        .arg(serde_json::to_string(&[args.sops_key])?)
        .arg(args.sops_file)
        .output()?;
    if !output.status.success() {
        return Err(format!(
            "SOPS decryption failed ({}): {}",
            output.status,
            String::from_utf8_lossy(&output.stderr).trim_end(),
        )
        .into());
    }

    let (_, session): (Variant<String>, Path<'static>) = service.method_call(
        "org.freedesktop.Secret.Service",
        "OpenSession",
        ("plain", Variant("")),
    )?;
    // GNOME's private API unlocks the existing collection without a GUI prompt
    // or replacing its daemon. Password bytes stay out of argv and the environment.
    let () = service.method_call(
        "org.gnome.keyring.InternalUnsupportedGuiltRiddenInterface",
        "UnlockWithMasterPassword",
        (
            collection,
            (session, Vec::<u8>::new(), output.stdout, "text/plain"),
        ),
    )?;
    if keyring.get::<bool>("org.freedesktop.Secret.Collection", "Locked")? {
        return Err("Login keyring is still locked after the unlock attempt".into());
    }
    Ok(())
}
