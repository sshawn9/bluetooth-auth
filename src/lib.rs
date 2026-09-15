use std::{
    error::Error,
    fs::{self, TryLockError},
    io,
    os::{
        fd::{AsRawFd, FromRawFd, OwnedFd},
        unix::fs::OpenOptionsExt,
    },
    process::Stdio,
    time::Duration,
};

use bluer::Address;
use bluer::adv::{Advertisement, AdvertisementHandle, Type};
use bluer::gatt::local::{
    Application, ApplicationHandle, Characteristic, CharacteristicNotify, CharacteristicRead,
    CharacteristicWrite, CharacteristicWriteMethod, Descriptor, DescriptorRead, ReqError, Service,
};
use tokio::{io::AsyncWriteExt, process::Command, runtime::Builder, time};

pub const LOCK_PATH: &str = "/run/bluetooth-auth/hci0.lock";

/// Prefer LE for the target, then disconnect BR/EDR and remove its pairing.
/// Log preparation errors once; skip unpairing if disconnection fails.
pub fn prepare_le(target: Address) -> bool {
    let preferred = prefer_le(target)
        .inspect_err(|error| eprintln!("Cannot prefer LE: {error}"))
        .is_ok();
    if let Err(error) = disconnect_classic(target) {
        eprintln!("Cannot disconnect BR/EDR; skipping unpairing: {error}");
        return false;
    }
    let unpaired = remove_classic_pairing(target)
        .inspect_err(|error| eprintln!("Cannot remove BR/EDR pairing: {error}"))
        .is_ok();
    preferred && unpaired
}

/// Disconnect only the target's BR/EDR link on hci0; already disconnected is success.
pub fn disconnect_classic(target: Address) -> Result<(), Box<dyn Error>> {
    classic_request(target, 0x0014, &[0x02, 0x0e]) // Not Connected / Disconnected
}

/// Remove only the target's BR/EDR bond on hci0.
/// Disconnect BR/EDR first: BlueZ may otherwise also disconnect an existing LE link.
/// Already unpaired is success. Bluetoothd must be running to persist the kernel's unpair event.
pub fn remove_classic_pairing(target: Address) -> Result<(), Box<dyn Error>> {
    classic_request(target, 0x001b, &[0x06]) // Unpair Device / Not Paired
}

/// Prefer LE for an existing target on hci0; an absent device is skipped successfully.
/// Requires BlueZ's experimental PreferredBearer property when the device exists.
/// Setting the preference can also enable BlueZ's native LE auto-connect.
pub fn prefer_le(target: Address) -> Result<(), Box<dyn Error>> {
    use dbus::blocking::{
        Connection,
        stdintf::org_freedesktop_dbus::{ObjectManager, Properties},
    };

    let connection = Connection::new_system()?;
    let target = target.to_string();
    let bluez = connection.with_proxy("org.bluez", "/", Duration::from_secs(5));
    let Some(path) = bluez
        .get_managed_objects()?
        .into_iter()
        .find_map(|(path, interfaces)| {
            let properties = interfaces.get("org.bluez.Device1")?;
            let address = dbus::arg::prop_cast::<String>(properties, "Address")?;
            (path.starts_with("/org/bluez/hci0/") && address.eq_ignore_ascii_case(&target))
                .then_some(path)
        })
    else {
        return Ok(());
    };
    let device = connection.with_proxy("org.bluez", path, Duration::from_secs(5));
    device.set("org.bluez.Device1", "PreferredBearer", "le")?;
    Ok(())
}

// One request per control socket. The kernel routes its reply to this socket;
// bluetoothd receives the separate unpair event and updates its stored LinkKey.
fn classic_request(
    target: Address,
    opcode: u16,
    absent_statuses: &[u8],
) -> Result<(), Box<dyn Error>> {
    // SAFETY: Create an HCI control socket; OwnedFd below owns it on every subsequent path.
    let fd = unsafe {
        libc::socket(
            libc::AF_BLUETOOTH,
            libc::SOCK_RAW | libc::SOCK_CLOEXEC | libc::SOCK_NONBLOCK,
            1, // BTPROTO_HCI
        )
    };
    if fd < 0 {
        return Err(io::Error::last_os_error().into());
    }
    // SAFETY: fd was just created and has no other owner.
    let socket = unsafe { OwnedFd::from_raw_fd(fd) };
    let address = [libc::AF_BLUETOOTH as u16, 0xffff, 3]; // sockaddr_hci: NONE / CONTROL
    // SAFETY: address has the native sockaddr_hci layout and the supplied length.
    if unsafe {
        libc::bind(
            socket.as_raw_fd(),
            address.as_ptr().cast(),
            size_of_val(&address) as libc::socklen_t,
        )
    } < 0
    {
        return Err(io::Error::last_os_error().into());
    }

    let payload_len = if opcode == 0x001b { 8 } else { 7 };
    let mut request = [0u8; 14];
    request[..2].copy_from_slice(&opcode.to_le_bytes());
    request[4..6].copy_from_slice(&(payload_len as u16).to_le_bytes());
    request[6..12].copy_from_slice(&target.0);
    request[6..12].reverse();
    // Index=0, address type=0 (BR/EDR), and Unpair's Disconnect=0 are already zero.
    let request = &request[..6 + payload_len];
    // SAFETY: request is readable for its full length; socket remains owned here.
    let sent = unsafe {
        libc::send(
            socket.as_raw_fd(),
            request.as_ptr().cast(),
            request.len(),
            0,
        )
    };
    if sent < 0 {
        return Err(io::Error::last_os_error().into());
    }
    if sent as usize != request.len() {
        return Err(io::Error::new(io::ErrorKind::WriteZero, "Incomplete MGMT command").into());
    }

    let deadline = std::time::Instant::now() + Duration::from_secs(5);
    let mut pollfd = libc::pollfd {
        fd: socket.as_raw_fd(),
        events: libc::POLLIN,
        revents: 0,
    };
    let mut buffer = [0u8; 1024];
    loop {
        let remaining = deadline.saturating_duration_since(std::time::Instant::now());
        if remaining.is_zero() {
            return Err(io::Error::new(io::ErrorKind::TimedOut, "MGMT command timed out").into());
        }
        // SAFETY: pollfd points to one initialized poll descriptor.
        let ready = unsafe { libc::poll(&mut pollfd, 1, remaining.as_millis().max(1) as i32) };
        if ready < 0 {
            let error = io::Error::last_os_error();
            if error.kind() == io::ErrorKind::Interrupted {
                continue;
            }
            return Err(error.into());
        }
        if ready == 0 {
            return Err(io::Error::new(io::ErrorKind::TimedOut, "MGMT command timed out").into());
        }
        // SAFETY: buffer is writable for its full length; recv never writes beyond it.
        let size = unsafe {
            libc::recv(
                socket.as_raw_fd(),
                buffer.as_mut_ptr().cast(),
                buffer.len(),
                0,
            )
        };
        if size < 0 {
            return Err(io::Error::last_os_error().into());
        }
        let reply = &buffer[..size as usize];
        if reply.len() < 6 {
            return Err(io::Error::new(io::ErrorKind::InvalidData, "Truncated MGMT header").into());
        }
        let event = u16::from_le_bytes([reply[0], reply[1]]);
        if !matches!(event, 0x0001 | 0x0002) || reply[2..4] != [0, 0] {
            continue;
        }
        if reply.len() < 9
            || usize::from(u16::from_le_bytes([reply[4], reply[5]])) + 6 != reply.len()
        {
            return Err(
                io::Error::new(io::ErrorKind::InvalidData, "Invalid MGMT response length").into(),
            );
        }
        if reply[6..8] != opcode.to_le_bytes() {
            continue;
        }
        let status = reply[8];
        if absent_statuses.contains(&status) {
            return Ok(());
        }
        if status != 0 {
            return Err(
                format!("MGMT command 0x{opcode:04x} failed (status 0x{status:02x})").into(),
            );
        }
        if event == 0x0002 {
            // Command Status is not Command Complete.
            continue;
        }
        if reply[9..] != request[6..13] {
            return Err(
                io::Error::new(io::ErrorKind::InvalidData, "MGMT reply target mismatch").into(),
            );
        }
        return Ok(());
    }
}

/// Use `None` for manual enrollment, or `Some(address)` to restrict HID access.
pub async fn register_hid(target_address: Option<Address>) -> bluer::Result<ApplicationHandle> {
    let adapter = bluer::Session::new().await?.adapter("hci0")?;
    let access_adapter = bluer::Session::new().await?.adapter("hci0")?;
    let authorize = move |adapter_name: String, address: Address| {
        let adapter = access_adapter.clone();
        async move {
            if adapter_name != adapter.name() {
                return Err(ReqError::NotAuthorized);
            }
            let Some(target_address) = target_address else {
                return Ok(());
            };
            let device = adapter.device(address).map_err(|_| ReqError::Failed)?;
            let identity = device
                .remote_address()
                .await
                .map_err(|_| ReqError::Failed)?;
            if identity == target_address {
                Ok(())
            } else {
                Err(ReqError::NotAuthorized)
            }
        }
    };
    let uuid_base = 0x00000000_0000_1000_8000_00805f9b34fb_u128;

    let hid = (
        0x1812_u16,
        vec![
            (0x2a4a_u16, vec![0x11, 0x01, 0x00, 0x02]),
            (
                0x2a4b,
                vec![
                    0x05, 0x0c, 0x09, 0x01, 0xa1, 0x01, 0x85, 0x01, 0x15, 0x00, 0x25, 0x01, 0x75,
                    0x01, 0x95, 0x08, 0x09, 0xcd, 0x09, 0xb5, 0x09, 0xb6, 0x09, 0xb7, 0x09, 0xe9,
                    0x09, 0xea, 0x09, 0xe2, 0x09, 0x40, 0x81, 0x02, 0xc0,
                ],
            ),
            (0x2a4c, vec![0x00]),
            (0x2a4d, vec![0x00]),
        ],
    );
    let battery = (0x180f, vec![(0x2a19, vec![0x64])]);
    let device_info = (
        0x180a,
        vec![(0x2a50, vec![0x02, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00])],
    );

    let mut services = Vec::new();
    for (service_uuid, values) in [hid, battery, device_info] {
        let mut characteristics = Vec::new();
        for (uuid, value) in values {
            let check = authorize.clone();
            let mut characteristic = Characteristic {
                uuid: bluer::Uuid::from_u128(uuid_base | (u128::from(uuid) << 96)),
                ..Default::default()
            };
            if uuid == 0x2a4c {
                characteristic.write = Some(CharacteristicWrite {
                    write_without_response: true,
                    encrypt_write: true,
                    method: CharacteristicWriteMethod::Fun(Box::new(move |_, request| {
                        Box::pin(check(request.adapter_name, request.device_address))
                    })),
                    ..Default::default()
                });
            } else {
                characteristic.read = Some(CharacteristicRead {
                    read: true,
                    encrypt_read: service_uuid != 0x180a,
                    fun: Box::new(move |request| {
                        let authorized = check(request.adapter_name, request.device_address);
                        let value = value
                            .get(usize::from(request.offset)..)
                            .map(<[u8]>::to_vec)
                            .ok_or(ReqError::InvalidOffset);
                        Box::pin(async move {
                            authorized.await?;
                            value
                        })
                    }),
                    ..Default::default()
                });
            }
            if matches!(uuid, 0x2a4d | 0x2a19) {
                characteristic.notify = Some(CharacteristicNotify {
                    notify: true,
                    ..Default::default()
                });
            }
            if uuid == 0x2a4d {
                let check = authorize.clone();
                characteristic.descriptors.push(Descriptor {
                    uuid: bluer::Uuid::from_u128(uuid_base | (0x2908_u128 << 96)),
                    read: Some(DescriptorRead {
                        read: true,
                        encrypt_read: true,
                        fun: Box::new(move |request| {
                            let authorized = check(request.adapter_name, request.device_address);
                            Box::pin(async move {
                                authorized.await?;
                                [0x01_u8, 0x01]
                                    .get(usize::from(request.offset)..)
                                    .map(<[u8]>::to_vec)
                                    .ok_or(ReqError::InvalidOffset)
                            })
                        }),
                        ..Default::default()
                    }),
                    ..Default::default()
                });
            }
            characteristics.push(characteristic);
        }
        services.push(Service {
            uuid: bluer::Uuid::from_u128(uuid_base | (u128::from(service_uuid) << 96)),
            primary: true,
            characteristics,
            ..Default::default()
        });
    }

    adapter
        .serve_gatt_application(Application {
            services,
            ..Default::default()
        })
        .await
}

pub async fn advertise() -> bluer::Result<AdvertisementHandle> {
    let session = bluer::Session::new().await?;
    let adapter = session.adapter("hci0")?;
    adapter
        .advertise(Advertisement {
            advertisement_type: Type::Peripheral,
            discoverable: Some(true),
            service_uuids: [bluer::Uuid::from_u128(
                0x00001812_0000_1000_8000_00805f9b34fb,
            )]
            .into(),
            local_name: Some(adapter.alias().await?),
            appearance: Some(0x03c0),
            min_interval: Some(Duration::from_millis(20)),
            max_interval: Some(Duration::from_millis(20)),
            ..Default::default()
        })
        .await
}

pub fn query_connection(target_address: Address) -> bluer::Result<bool> {
    let mut target = target_address.0;
    target.reverse(); // Linux bdaddr_t stores the least significant address byte first.

    // Linux hci_sock.h: a 4-byte header followed by 16-byte hci_conn_info entries.
    const CAPACITY: u16 = 512;
    let mut buffer = [0u8; 4 + 16 * CAPACITY as usize];
    buffer[2..4].copy_from_slice(&CAPACITY.to_ne_bytes()); // hci0 has dev_id 0.
    // SAFETY: Only opens an HCI socket; does not bind an adapter or send Bluetooth commands.
    let fd = unsafe { libc::socket(libc::AF_BLUETOOTH, libc::SOCK_RAW | libc::SOCK_CLOEXEC, 1) };
    if fd < 0 {
        return Err(io::Error::last_os_error().into());
    }
    // SAFETY: The new fd is owned exactly once and closed on every return path.
    let socket = unsafe { OwnedFd::from_raw_fd(fd) };
    // SAFETY: The header is initialized, and the buffer can hold all requested connection entries.
    if unsafe {
        libc::ioctl(
            socket.as_raw_fd(),
            libc::_IOR::<libc::c_int>(u32::from(b'H'), 212),
            buffer.as_mut_ptr(),
        )
    } < 0
    {
        return Err(io::Error::last_os_error().into());
    }
    let count = u16::from_ne_bytes([buffer[2], buffer[3]]);
    if u16::from_ne_bytes([buffer[0], buffer[1]]) != 0 || count >= CAPACITY {
        return Err(io::Error::other("Incomplete kernel Bluetooth connection snapshot").into());
    }
    let mut matches = buffer[4..4 + 16 * usize::from(count)]
        .as_chunks::<16>()
        .0
        .iter()
        .filter(|entry| {
            entry[2..8] == target
                && entry[8] == 0x80 // LE_LINK
                && u16::from_ne_bytes([entry[10], entry[11]]) == 1 // BT_CONNECTED
        });
    let connection = matches.next();
    if matches.next().is_some() {
        return Err(io::Error::other("Cannot identify a unique target LE connection").into());
    }
    Ok(connection.is_some_and(|entry| {
        u32::from_ne_bytes([entry[12], entry[13], entry[14], entry[15]]) & 0x0004 != 0 // HCI_LM_ENCRYPT
    }))
}

pub fn query_or_connect(target: Address, timeout_ms: u64) -> Result<bool, Box<dyn Error>> {
    if query_connection(target)? {
        return Ok(true);
    }
    let deadline = time::Instant::now()
        .checked_add(Duration::from_millis(timeout_ms))
        .ok_or("Connection timeout is too large")?;
    let lock_interval = Duration::from_millis((timeout_ms / 20).max(100));
    // Provision this file once; never replace or unlink a lock that another process may hold.
    let lock = fs::File::options()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(LOCK_PATH)
        .map_err(|error| format!("Cannot open connection lock {LOCK_PATH}: {error}"))?;
    let runtime = Builder::new_current_thread().enable_all().build()?;
    let result = runtime.block_on(async {
        // Keep the child outside the timed future so timeout cleanup can reap it.
        let mut preparation = None;
        let result = time::timeout_at(deadline, async {
            loop {
                match lock.try_lock() {
                    Ok(()) => break,
                    Err(TryLockError::WouldBlock) => time::sleep(lock_interval).await,
                    Err(TryLockError::Error(error)) => return Err(error.into()),
                }
            }
            if query_connection(target)? {
                return Ok(true);
            }
            match Command::new("/run/wrappers/bin/bluetooth-auth-prepare-le")
                .stdin(Stdio::piped())
                .stdout(Stdio::null())
                .kill_on_drop(true)
                .spawn()
            {
                Ok(child) => {
                    preparation = Some(child);
                    if let Some(child) = preparation.as_mut() {
                        if let Some(mut input) = child.stdin.take()
                            && let Err(error) =
                                input.write_all(format!("{target}\n").as_bytes()).await
                        {
                            eprintln!("Cannot send LE preparation target: {error}");
                            child.kill().await?;
                        }
                        // The helper logs step failures; its exit status does not gate HID.
                        if let Err(error) = child.wait().await {
                            eprintln!("Cannot wait for LE preparation: {error}");
                            child.kill().await?;
                        }
                    }
                    preparation = None;
                }
                Err(error) => eprintln!("Cannot start LE preparation: {error}"),
            }
            let _hid = register_hid(Some(target)).await?;
            let _advertisement = advertise().await?;
            while !query_connection(target)? {
                time::sleep(Duration::from_millis(10)).await;
            }
            Ok::<bool, Box<dyn Error>>(true)
        })
        .await;
        if let Some(mut child) = preparation {
            // Kill and reap before dropping the runtime or releasing the connection lock.
            child.kill().await?;
        }
        result.unwrap_or(Ok(false))
    });
    // Close all D-Bus connections, including partial registrations, before releasing the lock.
    drop(runtime);
    result
}
