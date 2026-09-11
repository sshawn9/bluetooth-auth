use bluetooth_auth::{advertise, register_hid};

#[tokio::main(flavor = "current_thread")]
async fn main() -> bluer::Result<()> {
    eprintln!("Open your computer's Bluetooth pairing tool and allow new pairings.");
    eprintln!("Registering a temporary HID service on hci0");
    let _hid = register_hid(None).await?;
    eprintln!("HID service registered; starting advertising");
    let _advertisement = advertise().await?;
    eprintln!("Advertising started. Select this computer on your iPhone and complete pairing.");
    eprintln!(
        "Runs until Ctrl+C. Exiting releases temporary HID and advertising, keeping the pairing."
    );
    std::future::pending().await
}
