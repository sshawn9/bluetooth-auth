use std::{
    error::Error,
    fs::{self, TryLockError},
    io,
    os::{
        fd::{AsRawFd, FromRawFd, OwnedFd},
        unix::fs::OpenOptionsExt,
    },
    time::Duration,
};

use bluer::Address;
use bluer::adv::{Advertisement, AdvertisementHandle, Type};
use bluer::gatt::local::{
    Application, ApplicationHandle, Characteristic, CharacteristicNotify, CharacteristicRead,
    CharacteristicWrite, CharacteristicWriteMethod, Descriptor, DescriptorRead, ReqError, Service,
};
use tokio::{runtime::Builder, time};

const LOCK_PATH: &str = "/run/bluetooth-auth/hci0.lock";

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
        time::timeout_at(deadline, async {
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
            let _hid = register_hid(Some(target)).await?;
            let _advertisement = advertise().await?;
            while !query_connection(target)? {
                time::sleep(Duration::from_millis(10)).await;
            }
            Ok::<bool, Box<dyn Error>>(true)
        })
        .await
    });
    // Close all D-Bus connections, including partial registrations, before releasing the lock.
    drop(runtime);
    result.unwrap_or(Ok(false))
}
