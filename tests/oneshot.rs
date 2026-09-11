//! Executable checks on a private D-Bus and synthetic HCI.
#![cfg(target_os = "linux")]

use std::{
    fs,
    os::unix::process::ExitStatusExt,
    path::{Path, PathBuf},
    process::{Child, Command, Output, Stdio},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicU8, Ordering},
        mpsc,
    },
    thread,
    time::{Duration, Instant},
};

use dbus::{
    Path as DbusPath, arg::PropMap, blocking::Connection, channel::MatchingReceiver,
    message::MatchRule,
};
use dbus_crossroads::Crossroads;

mod support;
use support::PrivateBus;

const TARGET: &str = "02:00:00:00:00:01";

#[derive(Clone, Copy, Eq, PartialEq)]
#[repr(u8)]
enum Behavior {
    Accept,
    RejectGatt,
    RejectAdvertisement,
    HangAdvertisement,
}

#[derive(Clone, Debug)]
struct Registration {
    owner: String,
    path: DbusPath<'static>,
}

#[derive(Clone, Debug, Default)]
struct Calls {
    gatt: Vec<Registration>,
    advertisements: Vec<Registration>,
    unregister_gatt: usize,
    unregister_advertisements: usize,
    methods: Vec<String>,
}

#[derive(Clone)]
struct AdapterData {
    calls: Arc<Mutex<Calls>>,
    behavior: Arc<AtomicU8>,
}

struct FakeBluez {
    calls: Arc<Mutex<Calls>>,
    behavior: Arc<AtomicU8>,
    stop: Arc<AtomicBool>,
    thread: Option<thread::JoinHandle<()>>,
}

impl FakeBluez {
    fn start(address: &str) -> Self {
        let calls = Arc::new(Mutex::new(Calls::default()));
        let behavior = Arc::new(AtomicU8::new(Behavior::Accept as u8));
        let stop = Arc::new(AtomicBool::new(false));
        let (ready_tx, ready_rx) = mpsc::channel();
        let thread_calls = calls.clone();
        let thread_behavior = behavior.clone();
        let thread_stop = stop.clone();
        let address = address.to_owned();
        let thread = thread::spawn(move || {
            let connection = Connection::new_address(&address).unwrap();
            connection
                .request_name("org.bluez", false, true, false)
                .unwrap();
            let mut crossroads = Crossroads::new();

            let adapter = crossroads.register("org.bluez.Adapter1", |builder| {
                builder
                    .property::<String, _>("Alias")
                    .get(|_, _: &mut AdapterData| Ok("One-shot fake adapter".to_owned()));
            });
            let gatt = crossroads.register("org.bluez.GattManager1", |builder| {
                builder.method(
                    "RegisterApplication",
                    ("application", "options"),
                    (),
                    |context, data: &mut AdapterData, (path, _): (DbusPath<'static>, PropMap)| {
                        data.calls.lock().unwrap().gatt.push(Registration {
                            owner: context.message().sender().unwrap().to_string(),
                            path,
                        });
                        if data.behavior.load(Ordering::Acquire) == Behavior::RejectGatt as u8 {
                            Err(dbus::MethodErr::failed("simulated GATT rejection"))
                        } else {
                            Ok(())
                        }
                    },
                );
                builder.method(
                    "UnregisterApplication",
                    ("application",),
                    (),
                    |_, data: &mut AdapterData, (_path,): (DbusPath<'static>,)| {
                        data.calls.lock().unwrap().unregister_gatt += 1;
                        Ok(())
                    },
                );
            });
            let advertising = crossroads.register("org.bluez.LEAdvertisingManager1", |builder| {
                builder.method(
                    "RegisterAdvertisement",
                    ("advertisement", "options"),
                    (),
                    |context, data: &mut AdapterData, (path, _): (DbusPath<'static>, PropMap)| {
                        data.calls
                            .lock()
                            .unwrap()
                            .advertisements
                            .push(Registration {
                                owner: context.message().sender().unwrap().to_string(),
                                path,
                            });
                        if data.behavior.load(Ordering::Acquire)
                            == Behavior::RejectAdvertisement as u8
                        {
                            Err(dbus::MethodErr::failed("simulated advertisement rejection"))
                        } else {
                            Ok(())
                        }
                    },
                );
                builder.method(
                    "UnregisterAdvertisement",
                    ("advertisement",),
                    (),
                    |_, data: &mut AdapterData, (_path,): (DbusPath<'static>,)| {
                        data.calls.lock().unwrap().unregister_advertisements += 1;
                        Ok(())
                    },
                );
            });
            let data = AdapterData {
                calls: thread_calls.clone(),
                behavior: thread_behavior.clone(),
            };
            crossroads.insert("/org/bluez/hci0", &[adapter, gatt, advertising], data);

            connection.start_receive(
                MatchRule::new_method_call(),
                Box::new(move |message, connection| {
                    let member = message
                        .member()
                        .map(|member| member.to_string())
                        .unwrap_or_default();
                    thread_calls.lock().unwrap().methods.push(member.clone());
                    let hang_advertisement = thread_behavior.load(Ordering::Acquire)
                        == Behavior::HangAdvertisement as u8
                        && member == "RegisterAdvertisement";
                    if hang_advertisement {
                        let (path, _): (DbusPath<'static>, PropMap) = message.read2().unwrap();
                        thread_calls
                            .lock()
                            .unwrap()
                            .advertisements
                            .push(Registration {
                                owner: message.sender().unwrap().to_string(),
                                path,
                            });
                        return true;
                    }
                    crossroads.handle_message(message, connection).unwrap();
                    true
                }),
            );
            ready_tx.send(()).unwrap();
            while !thread_stop.load(Ordering::Acquire) {
                connection.process(Duration::from_millis(10)).unwrap();
            }
        });
        ready_rx.recv_timeout(Duration::from_secs(2)).unwrap();
        Self {
            calls,
            behavior,
            stop,
            thread: Some(thread),
        }
    }

    fn reset(&self, behavior: Behavior) {
        *self.calls.lock().unwrap() = Calls::default();
        self.behavior.store(behavior as u8, Ordering::Release);
    }

    fn snapshot(&self) -> Calls {
        self.calls.lock().unwrap().clone()
    }

    fn wait_for_registrations(&self) -> Calls {
        let deadline = Instant::now() + Duration::from_secs(2);
        loop {
            let calls = self.snapshot();
            if calls.gatt.len() == 1 && calls.advertisements.len() == 1 {
                return calls;
            }
            assert!(
                Instant::now() < deadline,
                "registrations did not arrive: {calls:?}"
            );
            thread::sleep(Duration::from_millis(10));
        }
    }
}

impl Drop for FakeBluez {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
        if let Some(thread) = self.thread.take() {
            thread.join().unwrap();
        }
    }
}

fn compile_shim(directory: &Path) -> PathBuf {
    let shim = directory.join("oneshot_hci.so");
    let source = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/support/oneshot_hci.c");
    let output = Command::new("cc")
        .args(["-shared", "-fPIC", "-O2", "-Wall", "-Wextra"])
        .arg("-o")
        .arg(&shim)
        .arg(source)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "failed to compile HCI shim:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
    shim
}

fn query_count(path: &Path) -> usize {
    fs::read_to_string(path).unwrap_or_default().lines().count()
}

struct BoundedChild(Option<Child>);

impl BoundedChild {
    fn spawn(command: &mut Command) -> Self {
        Self(Some(command.spawn().unwrap()))
    }

    fn id(&self) -> u32 {
        self.0.as_ref().unwrap().id()
    }

    fn wait(mut self, timeout: Duration) -> Output {
        let deadline = Instant::now() + timeout;
        loop {
            if self.0.as_mut().unwrap().try_wait().unwrap().is_some() {
                return self.0.take().unwrap().wait_with_output().unwrap();
            }
            if Instant::now() >= deadline {
                let mut child = self.0.take().unwrap();
                let _ = child.kill();
                let output = child.wait_with_output().unwrap();
                panic!("one-shot child exceeded test deadline: {output:?}");
            }
            thread::sleep(Duration::from_millis(10));
        }
    }
}

impl Drop for BoundedChild {
    fn drop(&mut self) {
        if let Some(child) = self.0.as_mut() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

fn command(
    bus: &PrivateBus,
    shim: &Path,
    phone: &Path,
    log: &Path,
    hci_scenario: &str,
    timeout_seconds: u32,
) -> Command {
    fs::write(log, b"").unwrap();
    let mut command = Command::new(env!("CARGO_BIN_EXE_ble-ask-or-connect"));
    command
        .args(["--address-file", phone.to_str().unwrap()])
        .args(["--timeout-seconds", &timeout_seconds.to_string()])
        .env("DBUS_SYSTEM_BUS_ADDRESS", &bus.address)
        .env("LD_PRELOAD", shim)
        .env("BT_AUTH_HCI_SCENARIO", hci_scenario)
        .env("BT_AUTH_HCI_LOG", log)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    command
}

fn run(
    bus: &PrivateBus,
    shim: &Path,
    phone: &Path,
    log: &Path,
    hci_scenario: &str,
    timeout_seconds: u32,
) -> Output {
    let mut command = command(bus, shim, phone, log, hci_scenario, timeout_seconds);
    BoundedChild::spawn(&mut command).wait(Duration::from_secs(4))
}

fn assert_registration_counts(calls: &Calls, gatt: usize, advertisements: usize) {
    assert_eq!(calls.gatt.len(), gatt, "GATT registrations: {calls:?}");
    assert_eq!(
        calls.advertisements.len(),
        advertisements,
        "advertisement registrations: {calls:?}"
    );
}

fn assert_no_forbidden_methods(calls: &Calls) {
    const FORBIDDEN: &[&str] = &[
        "Pair",
        "CancelPairing",
        "Connect",
        "Disconnect",
        "RemoveDevice",
        "Set",
        "SetDiscoveryFilter",
        "StartDiscovery",
        "StopDiscovery",
        "RegisterAgent",
        "RequestDefaultAgent",
    ];
    assert!(
        !calls
            .methods
            .iter()
            .any(|method| FORBIDDEN.contains(&method.as_str())),
        "executable made a forbidden call: {calls:?}"
    );
}

fn assert_owners_gone(bus: &PrivateBus, calls: &Calls) {
    let connection = Connection::new_address(&bus.address).unwrap();
    let proxy = connection.with_proxy(
        "org.freedesktop.DBus",
        "/org/freedesktop/DBus",
        Duration::from_secs(1),
    );
    for registration in calls.gatt.iter().chain(&calls.advertisements) {
        let deadline = Instant::now() + Duration::from_secs(1);
        loop {
            let (has_owner,): (bool,) = proxy
                .method_call(
                    "org.freedesktop.DBus",
                    "NameHasOwner",
                    (registration.owner.as_str(),),
                )
                .unwrap();
            if !has_owner {
                break;
            }
            assert!(
                Instant::now() < deadline,
                "registration owner {} for {} survived process exit",
                registration.owner,
                registration.path
            );
            thread::sleep(Duration::from_millis(10));
        }
    }
}

fn assert_failed(output: &Output, message: &str) {
    assert_eq!(output.status.code(), Some(1), "{message}: {output:?}");
    assert!(
        !output.stderr.is_empty(),
        "{message}: expected an error on stderr"
    );
    assert!(
        output.stdout.is_empty(),
        "{message}: {}",
        String::from_utf8_lossy(&output.stdout)
    );
}

#[test]
fn oneshot_executable_is_bounded_and_releases_dbus_owners() {
    let bus = PrivateBus::start();
    let shim = compile_shim(&bus.directory);
    let phone = bus.directory.join("phone");
    let log = bus.directory.join("hci.log");
    fs::write(&phone, format!("{TARGET}\n")).unwrap();
    let fake = FakeBluez::start(&bus.address);

    fake.reset(Behavior::Accept);
    let output = run(&bus, &shim, &phone, &log, "encrypted", 1);
    assert!(output.status.success(), "fast path failed: {output:?}");
    assert_eq!(query_count(&log), 1);
    let calls = fake.snapshot();
    assert_registration_counts(&calls, 0, 0);
    assert_no_forbidden_methods(&calls);

    fake.reset(Behavior::Accept);
    let output = run(&bus, &shim, &phone, &log, "delayed", 1);
    assert!(
        output.status.success(),
        "delayed success failed: {output:?}"
    );
    assert!(query_count(&log) > 1);
    let calls = fake.snapshot();
    assert_registration_counts(&calls, 1, 1);
    assert_no_forbidden_methods(&calls);
    assert_owners_gone(&bus, &calls);

    fake.reset(Behavior::Accept);
    let started = Instant::now();
    let output = run(&bus, &shim, &phone, &log, "unencrypted", 1);
    assert_failed(&output, "unencrypted link should time out");
    assert!(started.elapsed() < Duration::from_secs(3));
    assert!(query_count(&log) > 1);
    let calls = fake.snapshot();
    assert_registration_counts(&calls, 1, 1);
    assert_no_forbidden_methods(&calls);
    assert_owners_gone(&bus, &calls);

    fake.reset(Behavior::Accept);
    let output = run(&bus, &shim, &phone, &log, "error", 1);
    assert_failed(&output, "HCI query error should fail");
    assert_eq!(query_count(&log), 1);
    let calls = fake.snapshot();
    assert_registration_counts(&calls, 0, 0);
    assert_no_forbidden_methods(&calls);

    fake.reset(Behavior::Accept);
    let output = run(&bus, &shim, &phone, &log, "late-error", 1);
    assert_failed(&output, "HCI query error after registration should fail");
    assert_eq!(query_count(&log), 2);
    let calls = fake.snapshot();
    assert_registration_counts(&calls, 1, 1);
    assert_no_forbidden_methods(&calls);
    assert_owners_gone(&bus, &calls);

    fake.reset(Behavior::RejectGatt);
    let output = run(&bus, &shim, &phone, &log, "disconnected", 1);
    assert_failed(&output, "GATT rejection should fail");
    let calls = fake.snapshot();
    assert_registration_counts(&calls, 1, 0);
    assert_no_forbidden_methods(&calls);
    assert_owners_gone(&bus, &calls);

    fake.reset(Behavior::RejectAdvertisement);
    let output = run(&bus, &shim, &phone, &log, "disconnected", 1);
    assert_failed(&output, "advertisement rejection should fail");
    let calls = fake.snapshot();
    assert_registration_counts(&calls, 1, 1);
    assert_no_forbidden_methods(&calls);
    assert_owners_gone(&bus, &calls);

    fake.reset(Behavior::HangAdvertisement);
    let started = Instant::now();
    let output = run(&bus, &shim, &phone, &log, "disconnected", 1);
    assert_failed(
        &output,
        "partial advertisement registration should time out",
    );
    assert!(started.elapsed() < Duration::from_secs(3));
    let calls = fake.snapshot();
    assert_registration_counts(&calls, 1, 1);
    assert_no_forbidden_methods(&calls);
    assert_owners_gone(&bus, &calls);

    for signal in [libc::SIGINT, libc::SIGTERM] {
        fake.reset(Behavior::Accept);
        let mut process = command(&bus, &shim, &phone, &log, "disconnected", 10);
        let child = BoundedChild::spawn(&mut process);
        fake.wait_for_registrations();
        assert_eq!(unsafe { libc::kill(child.id() as i32, signal) }, 0);
        let output = child.wait(Duration::from_secs(2));
        assert_eq!(output.status.signal(), Some(signal), "{output:?}");
        let calls = fake.snapshot();
        assert_registration_counts(&calls, 1, 1);
        assert_no_forbidden_methods(&calls);
        assert_owners_gone(&bus, &calls);
    }
}

#[test]
fn hid_server_waits_for_interrupt_and_releases_dbus_owners() {
    let bus = PrivateBus::start();
    let shim = compile_shim(&bus.directory);
    let log = bus.directory.join("hci.log");
    let fake = FakeBluez::start(&bus.address);
    let mut command = Command::new(env!("CARGO_BIN_EXE_ble-hid-server"));
    command
        .env("DBUS_SYSTEM_BUS_ADDRESS", &bus.address)
        .env("LD_PRELOAD", shim)
        .env("BT_AUTH_HCI_SCENARIO", "error")
        .env("BT_AUTH_HCI_LOG", &log)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = BoundedChild::spawn(&mut command);
    fake.wait_for_registrations();
    assert!(child.0.as_mut().unwrap().try_wait().unwrap().is_none());
    assert_eq!(unsafe { libc::kill(child.id() as i32, libc::SIGINT) }, 0);
    let output = child.wait(Duration::from_secs(2));
    assert_eq!(output.status.signal(), Some(libc::SIGINT), "{output:?}");
    assert_eq!(query_count(&log), 0);
    let calls = fake.snapshot();
    assert_registration_counts(&calls, 1, 1);
    assert_no_forbidden_methods(&calls);
    assert_owners_gone(&bus, &calls);
}
