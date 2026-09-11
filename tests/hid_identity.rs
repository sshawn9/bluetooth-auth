//! `register_hid` identity lookup against a private fake BlueZ bus.
#![cfg(target_os = "linux")]

use std::{
    collections::HashMap,
    process::Command,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    thread,
    time::{Duration, Instant},
};

use dbus::{
    Path,
    arg::{PropMap, Variant, prop_cast},
    blocking::Connection,
    channel::MatchingReceiver,
    message::MatchRule,
};
use dbus_crossroads::Crossroads;

mod support;
use support::PrivateBus;

const TARGET: &str = "AA:BB:CC:DD:EE:FF";
const OTHER: &str = "11:22:33:44:55:66";
const RPA_PATH: &str = "/org/bluez/hci0/dev_42_11_22_33_44_55";
const TARGET_LIKE_PATH: &str = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF";
const MISSING_PATH: &str = "/org/bluez/hci0/dev_DE_AD_BE_EF_00_01";
const OTHER_ADAPTER_PATH: &str = "/org/bluez/hci1/dev_42_11_22_33_44_55";

#[unsafe(export_name = "socket")]
unsafe extern "C" fn reject_bluetooth_socket(domain: i32, kind: i32, protocol: i32) -> i32 {
    if domain == libc::AF_BLUETOOTH {
        unsafe { *libc::__errno_location() = libc::EPERM };
        -1
    } else {
        unsafe { libc::syscall(libc::SYS_socket, domain, kind, protocol) as i32 }
    }
}

#[derive(Clone)]
struct Registration {
    owner: String,
    path: Path<'static>,
}

#[derive(Default)]
struct ManagerState {
    registration: Option<Registration>,
    unregister_calls: usize,
}

#[derive(Clone)]
struct DeviceState(Arc<Mutex<String>>);

impl DeviceState {
    fn new(address: &str) -> Self {
        Self(Arc::new(Mutex::new(address.to_owned())))
    }

    fn set(&self, address: &str) {
        *self.0.lock().unwrap() = address.to_owned();
    }
}

struct FakeBluez {
    state: Arc<Mutex<ManagerState>>,
    rpa: DeviceState,
    stop: Arc<AtomicBool>,
    thread: thread::JoinHandle<()>,
}

impl FakeBluez {
    fn start() -> Self {
        let state = Arc::new(Mutex::new(ManagerState::default()));
        let rpa = DeviceState::new(TARGET);
        let stop = Arc::new(AtomicBool::new(false));
        let (ready_tx, ready_rx) = mpsc::channel();
        let thread_state = state.clone();
        let thread_rpa = rpa.clone();
        let thread_stop = stop.clone();

        let thread = thread::spawn(move || {
            let connection = Connection::new_system().unwrap();
            connection
                .request_name("org.bluez", false, true, false)
                .unwrap();
            let mut crossroads = Crossroads::new();

            let manager = crossroads.register("org.bluez.GattManager1", move |builder| {
                builder.method(
                    "RegisterApplication",
                    ("application", "options"),
                    (),
                    |context,
                     data: &mut Arc<Mutex<ManagerState>>,
                     (path, _): (Path<'static>, PropMap)| {
                        let owner = context.message().sender().unwrap().to_string();
                        data.lock().unwrap().registration = Some(Registration { owner, path });
                        Ok(())
                    },
                );
                builder.method(
                    "UnregisterApplication",
                    ("application",),
                    (),
                    |_, data: &mut Arc<Mutex<ManagerState>>, (path,): (Path<'static>,)| {
                        let mut data = data.lock().unwrap();
                        assert_eq!(data.registration.as_ref().map(|r| &r.path), Some(&path));
                        data.unregister_calls += 1;
                        Ok(())
                    },
                );
            });
            let device = crossroads.register("org.bluez.Device1", |builder| {
                builder
                    .property::<String, _>("Address")
                    .get(|_, data: &mut DeviceState| Ok(data.0.lock().unwrap().clone()));
            });

            crossroads.insert("/org/bluez/hci0", &[manager], thread_state);
            crossroads.insert(RPA_PATH, &[device], thread_rpa);
            crossroads.insert(TARGET_LIKE_PATH, &[device], DeviceState::new(OTHER));

            connection.start_receive(
                MatchRule::new_method_call(),
                Box::new(move |message, connection| {
                    crossroads.handle_message(message, connection).unwrap();
                    true
                }),
            );
            ready_tx.send(()).unwrap();
            while !thread_stop.load(Ordering::Acquire) {
                connection.process(Duration::from_millis(20)).unwrap();
            }
        });
        ready_rx.recv_timeout(Duration::from_secs(2)).unwrap();
        Self {
            state,
            rpa,
            stop,
            thread,
        }
    }

    fn registration(&self) -> Registration {
        let deadline = Instant::now() + Duration::from_secs(2);
        loop {
            if let Some(registration) = self.state.lock().unwrap().registration.clone() {
                return registration;
            }
            assert!(
                Instant::now() < deadline,
                "RegisterApplication was not called"
            );
            thread::sleep(Duration::from_millis(10));
        }
    }

    fn finish(self) {
        self.stop.store(true, Ordering::Release);
        self.thread.join().unwrap();
    }
}

type ManagedObjects = HashMap<Path<'static>, HashMap<String, PropMap>>;

#[derive(Clone)]
struct AttributePaths {
    read: Path<'static>,
    write: Path<'static>,
    descriptor: Path<'static>,
}

fn attributes(connection: &Connection, registration: &Registration) -> AttributePaths {
    let proxy = connection.with_proxy(
        &registration.owner,
        &registration.path,
        Duration::from_secs(2),
    );
    let (objects,): (ManagedObjects,) = proxy
        .method_call(
            "org.freedesktop.DBus.ObjectManager",
            "GetManagedObjects",
            (),
        )
        .unwrap();
    let mut read = None;
    let mut write = None;
    let mut descriptor = None;
    for (path, interfaces) in objects {
        if let Some(properties) = interfaces.get("org.bluez.GattCharacteristic1") {
            match prop_cast::<String>(properties, "UUID").map(String::as_str) {
                Some("00002a4a-0000-1000-8000-00805f9b34fb") => read = Some(path.clone()),
                Some("00002a4c-0000-1000-8000-00805f9b34fb") => write = Some(path.clone()),
                _ => {}
            }
        }
        if let Some(properties) = interfaces.get("org.bluez.GattDescriptor1")
            && prop_cast::<String>(properties, "UUID").map(String::as_str)
                == Some("00002908-0000-1000-8000-00805f9b34fb")
        {
            descriptor = Some(path);
        }
    }
    AttributePaths {
        read: read.unwrap(),
        write: write.unwrap(),
        descriptor: descriptor.unwrap(),
    }
}

fn options(device: &str, write: bool) -> PropMap {
    let mut options: PropMap = HashMap::new();
    options.insert(
        "device".into(),
        Variant(Box::new(Path::new(device).unwrap())),
    );
    options.insert("offset".into(), Variant(Box::new(0u16)));
    options.insert("link".into(), Variant(Box::new("LE".to_owned())));
    options.insert("mtu".into(), Variant(Box::new(64u16)));
    if write {
        options.insert("type".into(), Variant(Box::new("command".to_owned())));
    }
    options
}

#[derive(Clone, Copy)]
enum Attribute {
    Read,
    Write,
    Descriptor,
}

fn invoke(
    connection: &Connection,
    registration: &Registration,
    paths: &AttributePaths,
    attribute: Attribute,
    device: &str,
) -> Result<(), dbus::Error> {
    match attribute {
        Attribute::Read => {
            let proxy =
                connection.with_proxy(&registration.owner, &paths.read, Duration::from_secs(2));
            let _: (Vec<u8>,) = proxy.method_call(
                "org.bluez.GattCharacteristic1",
                "ReadValue",
                (options(device, false),),
            )?;
        }
        Attribute::Write => {
            let proxy =
                connection.with_proxy(&registration.owner, &paths.write, Duration::from_secs(2));
            let _: () = proxy.method_call(
                "org.bluez.GattCharacteristic1",
                "WriteValue",
                (vec![0u8], options(device, true)),
            )?;
        }
        Attribute::Descriptor => {
            let proxy = connection.with_proxy(
                &registration.owner,
                &paths.descriptor,
                Duration::from_secs(2),
            );
            let _: (Vec<u8>,) = proxy.method_call(
                "org.bluez.GattDescriptor1",
                "ReadValue",
                (options(device, false),),
            )?;
        }
    }
    Ok(())
}

fn assert_error(result: Result<(), dbus::Error>, expected: &str) {
    let error = result.expect_err("request unexpectedly succeeded");
    assert_eq!(error.name(), Some(expected), "{error}");
}

async fn child_test() {
    let fake = FakeBluez::start();
    let target = TARGET.parse().unwrap();
    let handle = bluetooth_auth::register_hid(Some(target)).await.unwrap();
    let registration = fake.registration();
    let paths = tokio::task::spawn_blocking({
        let registration = registration.clone();
        move || {
            let connection = Connection::new_system().unwrap();
            attributes(&connection, &registration)
        }
    })
    .await
    .unwrap();

    for attribute in [Attribute::Read, Attribute::Write, Attribute::Descriptor] {
        let call_registration = registration.clone();
        let call_paths = paths.clone();
        tokio::task::spawn_blocking(move || {
            let connection = Connection::new_system().unwrap();

            // The path is an RPA, while Device1.Address is the stable target identity.
            invoke(
                &connection,
                &call_registration,
                &call_paths,
                attribute,
                RPA_PATH,
            )
            .unwrap();

            // A path that looks like the target is not authoritative.
            assert_error(
                invoke(
                    &connection,
                    &call_registration,
                    &call_paths,
                    attribute,
                    TARGET_LIKE_PATH,
                ),
                "org.bluez.Error.NotAuthorized",
            );

            // A failed Device1.Address lookup is a request failure, never authorization.
            assert_error(
                invoke(
                    &connection,
                    &call_registration,
                    &call_paths,
                    attribute,
                    MISSING_PATH,
                ),
                "org.bluez.Error.Failed",
            );

            // Reject another adapter before attempting an identity property lookup.
            assert_error(
                invoke(
                    &connection,
                    &call_registration,
                    &call_paths,
                    attribute,
                    OTHER_ADAPTER_PATH,
                ),
                "org.bluez.Error.NotAuthorized",
            );
        })
        .await
        .unwrap();

        // The next request must re-read Device1.Address instead of caching the first result.
        fake.rpa.set(OTHER);
        let changed_registration = registration.clone();
        let changed_paths = paths.clone();
        tokio::task::spawn_blocking(move || {
            let connection = Connection::new_system().unwrap();
            assert_error(
                invoke(
                    &connection,
                    &changed_registration,
                    &changed_paths,
                    attribute,
                    RPA_PATH,
                ),
                "org.bluez.Error.NotAuthorized",
            );
        })
        .await
        .unwrap();
        fake.rpa.set(TARGET);
    }

    drop(handle);
    let cleanup_state = fake.state.clone();
    tokio::task::spawn_blocking(move || {
        let deadline = Instant::now() + Duration::from_secs(2);
        loop {
            let unregister_calls = cleanup_state.lock().unwrap().unregister_calls;
            if unregister_calls == 1 {
                break;
            }
            assert!(
                Instant::now() < deadline,
                "GATT cleanup did not complete: unregister_calls={unregister_calls}"
            );
            thread::sleep(Duration::from_millis(10));
        }
    })
    .await
    .unwrap();

    // Enrollment accepts a device before its stable identity is available.
    let enrollment = bluetooth_auth::register_hid(None).await.unwrap();
    let registration = fake.registration();
    tokio::task::spawn_blocking(move || {
        let connection = Connection::new_system().unwrap();
        let paths = attributes(&connection, &registration);
        for attribute in [Attribute::Read, Attribute::Write, Attribute::Descriptor] {
            invoke(&connection, &registration, &paths, attribute, MISSING_PATH).unwrap();
            assert_error(
                invoke(
                    &connection,
                    &registration,
                    &paths,
                    attribute,
                    OTHER_ADAPTER_PATH,
                ),
                "org.bluez.Error.NotAuthorized",
            );
        }
    })
    .await
    .unwrap();
    drop(enrollment);
    fake.finish();
}

#[test]
fn hid_identity_is_read_from_device_property() {
    if std::env::var_os("BT_AUTH_HID_IDENTITY_CHILD").is_some() {
        let actual = std::env::var("DBUS_SYSTEM_BUS_ADDRESS").unwrap();
        let expected = std::env::var("BT_AUTH_HID_IDENTITY_BUS").unwrap();
        assert_eq!(actual, expected);
        assert!(actual.starts_with("unix:path="));
        assert!(actual.contains("bt-auth-test-"));
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap()
            .block_on(child_test());
        return;
    }

    let bus = PrivateBus::start();
    let output = Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "hid_identity_is_read_from_device_property",
            "--nocapture",
        ])
        .env("BT_AUTH_HID_IDENTITY_CHILD", "1")
        .env("DBUS_SYSTEM_BUS_ADDRESS", &bus.address)
        .env("BT_AUTH_HID_IDENTITY_BUS", &bus.address)
        .output()
        .unwrap();

    assert!(
        output.status.success(),
        "{}\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
}
