//! Executable checks on a private D-Bus and synthetic HCI.
#![cfg(target_os = "linux")]

use std::{
    fs::{self, OpenOptions},
    os::unix::{
        fs::{OpenOptionsExt, PermissionsExt},
        net::UnixDatagram,
        process::ExitStatusExt,
    },
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
    Message, Path as DbusPath,
    arg::{PropMap, Variant},
    blocking::Connection,
    channel::{MatchingReceiver, Sender},
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

#[derive(Clone, Debug, Eq, PartialEq)]
struct AdapterState {
    discoverable: bool,
    connectable: bool,
    pairable: bool,
    pairable_timeout: u32,
}

impl Default for AdapterState {
    fn default() -> Self {
        Self {
            discoverable: true,
            connectable: true,
            pairable: false,
            pairable_timeout: 42,
        }
    }
}

#[derive(Clone)]
struct AdapterData {
    calls: Arc<Mutex<Calls>>,
    behavior: Arc<AtomicU8>,
    state: Arc<Mutex<AdapterState>>,
}

struct FakeBluez {
    calls: Arc<Mutex<Calls>>,
    behavior: Arc<AtomicU8>,
    state: Arc<Mutex<AdapterState>>,
    signals: mpsc::Sender<(&'static str, PropMap)>,
    stop: Arc<AtomicBool>,
    thread: Option<thread::JoinHandle<()>>,
}

impl FakeBluez {
    fn start(address: &str) -> Self {
        let calls = Arc::new(Mutex::new(Calls::default()));
        let behavior = Arc::new(AtomicU8::new(Behavior::Accept as u8));
        let state = Arc::new(Mutex::new(AdapterState::default()));
        let stop = Arc::new(AtomicBool::new(false));
        let (ready_tx, ready_rx) = mpsc::channel();
        let (signals, received_signals) = mpsc::channel::<(&'static str, PropMap)>();
        let thread_calls = calls.clone();
        let thread_behavior = behavior.clone();
        let thread_state = state.clone();
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
                builder
                    .property::<bool, _>("Discoverable")
                    .get(|_, data: &mut AdapterData| Ok(data.state.lock().unwrap().discoverable))
                    .set(|_, data: &mut AdapterData, discoverable| {
                        let mut state = data.state.lock().unwrap();
                        state.discoverable = discoverable;
                        // BlueZ can make an adapter non-connectable as a side effect of
                        // disabling discoverability. The executable must restore both.
                        if !discoverable {
                            state.connectable = false;
                        }
                        Ok(Some(discoverable))
                    });
                builder
                    .property::<bool, _>("Connectable")
                    .get(|_, data: &mut AdapterData| Ok(data.state.lock().unwrap().connectable))
                    .set(|_, data: &mut AdapterData, connectable| {
                        data.state.lock().unwrap().connectable = connectable;
                        Ok(Some(connectable))
                    });
                builder
                    .property::<bool, _>("Pairable")
                    .get(|_, data: &mut AdapterData| Ok(data.state.lock().unwrap().pairable))
                    .set(|_, data: &mut AdapterData, pairable| {
                        data.state.lock().unwrap().pairable = pairable;
                        Ok(Some(pairable))
                    });
                builder
                    .property::<u32, _>("PairableTimeout")
                    .get(|_, data: &mut AdapterData| {
                        Ok(data.state.lock().unwrap().pairable_timeout)
                    })
                    .set(|_, data: &mut AdapterData, pairable_timeout| {
                        data.state.lock().unwrap().pairable_timeout = pairable_timeout;
                        Ok(Some(pairable_timeout))
                    });
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
                state: thread_state.clone(),
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
                for (path, properties) in received_signals.try_iter() {
                    connection
                        .send(
                            Message::new_signal(
                                path,
                                "org.freedesktop.DBus.Properties",
                                "PropertiesChanged",
                            )
                            .unwrap()
                            .append3(
                                "org.bluez.Adapter1",
                                properties,
                                Vec::<String>::new(),
                            ),
                        )
                        .unwrap();
                }
                connection.process(Duration::from_millis(10)).unwrap();
            }
        });
        ready_rx.recv_timeout(Duration::from_secs(2)).unwrap();
        Self {
            calls,
            behavior,
            state,
            signals,
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

    fn adapter_state(&self) -> AdapterState {
        self.state.lock().unwrap().clone()
    }

    fn emit(&self, path: &'static str, properties: PropMap) {
        self.signals.send((path, properties)).unwrap();
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
    let executable = |name: &str| {
        std::env::split_paths(&std::env::var_os("PATH").unwrap())
            .map(|path| path.join(name))
            .find(|path| path.is_file())
            .unwrap()
    };
    let helper = directory.join("prepare-le");
    fs::write(
        &helper,
        format!(
            r#"#!{}
set -eu
test "$#" -eq 0
IFS= read -r target
test "$target" = {TARGET}
printf '%s\n' "$$" >> "$BT_AUTH_RUNTIME_DIR/prepare.log"
case "${{BT_AUTH_PREPARE_MODE:-ok}}" in
  fail) echo 'simulated LE preparation failure' >&2; exit 1 ;;
  hang) exec {} 10 ;;
  delay) {} 0.6 ;;
esac
"#,
            executable("sh").display(),
            executable("sleep").display(),
            executable("sleep").display(),
        ),
    )
    .unwrap();
    fs::set_permissions(helper, fs::Permissions::from_mode(0o700)).unwrap();
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

fn link_command(
    bus: &PrivateBus,
    shim: &Path,
    phone: &Path,
    log: &Path,
    scenario: &str,
    connect: i64,
) -> Command {
    fs::write(log, b"").unwrap();
    let runtime = &bus.directory;
    provision_lock(runtime);
    let mut command = Command::new(env!("CARGO_BIN_EXE_bluetooth-auth-link"));
    command
        .args([
            "--address-file",
            phone.to_str().unwrap(),
            "--connect",
            &connect.to_string(),
        ])
        .env("DBUS_SYSTEM_BUS_ADDRESS", &bus.address)
        .env("LD_PRELOAD", shim)
        .env("BT_AUTH_RUNTIME_DIR", runtime)
        .env("BT_AUTH_HCI_SCENARIO", scenario)
        .env("BT_AUTH_HCI_LOG", log)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    command
}

fn provision_lock(runtime: &Path) {
    OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(runtime.join("hci0.lock"))
        .or_else(|_| {
            OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(runtime.join("hci0.lock"))
        })
        .unwrap();
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

fn assert_hid_no_forbidden_methods(calls: &Calls) {
    let mut calls = calls.clone();
    calls.methods.retain(|method| method != "Set");
    assert_no_forbidden_methods(&calls);
}

fn advertisement_is_discoverable(bus: &PrivateBus, calls: &Calls) -> bool {
    let registration = calls.advertisements.last().unwrap();
    let connection = Connection::new_address(&bus.address).unwrap();
    let proxy = connection.with_proxy(
        registration.owner.as_str(),
        registration.path.clone(),
        Duration::from_secs(1),
    );
    let (discoverable,): (Variant<bool>,) = proxy
        .method_call(
            "org.freedesktop.DBus.Properties",
            "Get",
            ("org.bluez.LEAdvertisement1", "Discoverable"),
        )
        .unwrap();
    discoverable.0
}

fn assert_adapter_restored(fake: &FakeBluez, initial: &AdapterState) {
    assert_eq!(
        &fake.adapter_state(),
        initial,
        "adapter state was not restored"
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
fn powered_events_query_or_connect_directly() {
    let bus = PrivateBus::start();
    let shim = compile_shim(&bus.directory);
    let phone = bus.directory.join("phone");
    let log = bus.directory.join("hci.log");
    let ready = bus.directory.join("connected");
    fs::write(&phone, format!("{TARGET}\n")).unwrap();
    fs::write(&ready, b"").unwrap();
    provision_lock(&bus.directory);
    let fake = FakeBluez::start(&bus.address);
    let mut command = Command::new(env!("CARGO_BIN_EXE_bluetooth-auth-power-monitor"));
    command
        .args([
            "--address-file",
            phone.to_str().unwrap(),
            "--timeout-ms",
            "200",
        ])
        .env("DBUS_SYSTEM_BUS_ADDRESS", &bus.address)
        .env("LD_PRELOAD", &shim)
        .env("BT_AUTH_RUNTIME_DIR", &bus.directory)
        .env("BT_AUTH_HCI_SCENARIO", "external")
        .env("BT_AUTH_HCI_READY_FILE", &ready)
        .env("BT_AUTH_HCI_LOG", &log)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = BoundedChild::spawn(&mut command);
    let connection = Connection::new_address(&bus.address).unwrap();
    let proxy = connection.with_proxy(
        "org.freedesktop.DBus",
        "/org/freedesktop/DBus",
        Duration::from_secs(1),
    );
    let deadline = Instant::now() + Duration::from_secs(2);
    loop {
        let (owned,): (bool,) = proxy
            .method_call(
                "org.freedesktop.DBus",
                "NameHasOwner",
                ("org.bluetooth_auth.PowerMonitor",),
            )
            .unwrap();
        if owned {
            break;
        }
        assert!(Instant::now() < deadline, "listener did not become ready");
        thread::sleep(Duration::from_millis(10));
    }
    let wait_for_query = |previous| {
        let deadline = Instant::now() + Duration::from_secs(2);
        while query_count(&log) <= previous {
            assert!(Instant::now() < deadline, "event did not query the link");
            thread::sleep(Duration::from_millis(10));
        }
    };
    let powered = |fake: &FakeBluez| {
        fake.emit(
            "/org/bluez/hci0",
            [("Powered".into(), Variant(Box::new(true) as _))].into(),
        );
    };

    // Startup, power-off, unrelated properties and other adapters never connect.
    fake.emit(
        "/org/bluez/hci0",
        [("Powered".into(), Variant(Box::new(false) as _))].into(),
    );
    fake.emit(
        "/org/bluez/hci0",
        [(
            "Alias".into(),
            Variant(Box::new("Fake adapter".to_owned()) as _),
        )]
        .into(),
    );
    fake.emit(
        "/org/bluez/hci1",
        [("Powered".into(), Variant(Box::new(true) as _))].into(),
    );
    thread::sleep(Duration::from_millis(100));
    assert_eq!(query_count(&log), 0);
    assert!(fake.snapshot().methods.is_empty());

    // An existing encrypted connection takes the library's query-only fast path.
    // No systemd service exists on this bus to perform the query for the listener.
    powered(&fake);
    wait_for_query(0);
    assert_eq!(query_count(&log), 1);
    assert!(fake.snapshot().methods.is_empty());

    // A missing connection gets one attempt, releases its resources on timeout,
    // and remains idle until another Powered event arrives.
    fs::remove_file(&ready).unwrap();
    powered(&fake);
    let calls = fake.wait_for_registrations();
    assert_owners_gone(&bus, &calls);
    let queries = query_count(&log);
    thread::sleep(Duration::from_millis(100));
    assert_eq!(query_count(&log), queries, "timeout must not retry");
    assert_registration_counts(&fake.snapshot(), 1, 1);
    assert_no_forbidden_methods(&fake.snapshot());

    // A registration error also leaves the listener alive for the next event.
    fake.reset(Behavior::RejectAdvertisement);
    powered(&fake);
    let calls = fake.wait_for_registrations();
    assert_owners_gone(&bus, &calls);
    let queries = query_count(&log);
    thread::sleep(Duration::from_millis(100));
    assert_eq!(query_count(&log), queries, "errors must not retry");

    // The next event can succeed and release HID without exiting the listener.
    fake.reset(Behavior::Accept);
    powered(&fake);
    let calls = fake.wait_for_registrations();
    fs::write(&ready, b"").unwrap();
    assert_owners_gone(&bus, &calls);
    assert_registration_counts(&fake.snapshot(), 1, 1);
    assert_no_forbidden_methods(&fake.snapshot());

    // The signal subscription still follows BlueZ after its bus owner changes.
    drop(fake);
    let fake = FakeBluez::start(&bus.address);
    let queries = query_count(&log);
    powered(&fake);
    wait_for_query(queries);
    assert_eq!(query_count(&log), queries + 1);
    assert!(fake.snapshot().methods.is_empty());

    child.0.as_mut().unwrap().kill().unwrap();
    let output = child.wait(Duration::from_secs(2));
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("simulated advertisement rejection"),
        "{output:?}"
    );
}

#[test]
fn noctalia_auto_lock_monitors_and_locks() {
    let bus = PrivateBus::start();
    let shim = compile_shim(&bus.directory);
    let phone = bus.directory.join("phone");
    let hci_log = bus.directory.join("hci.log");
    let noctalia_log = bus.directory.join("noctalia.log");
    let noctalia_state = bus.directory.join("noctalia.state");
    fs::write(&phone, format!("{TARGET}\n")).unwrap();
    let fake = FakeBluez::start(&bus.address);
    let shell = std::env::split_paths(&std::env::var_os("PATH").unwrap())
        .map(|directory| directory.join("sh"))
        .find(|candidate| candidate.is_file())
        .expect("a POSIX shell is required for the Noctalia mock");
    let noctalia_path = bus.directory.join("noctalia");
    fs::write(
        &noctalia_path,
        format!(
            "#!{}\n{}",
            shell.display(),
            r#"set -eu
case "$*" in
  'msg status') IFS= read -r state < "$NOCTALIA_STATE_FILE"; printf 'status\n' >> "$NOCTALIA_LOG"; printf '%s\n' "$state" ;;
  'msg session lock') printf 'lock\n' >> "$NOCTALIA_LOG"; test "$NOCTALIA_LOCK_FAIL" = 0 || { printf 'simulated lock failure\n'; exit 1; }; test "${NOCTALIA_LOCK_NOOP:-0}" = 0 || exit 0; printf '%s\n' '{"locked":true}' > "$NOCTALIA_STATE_FILE" ;;
  *) exit 64 ;;
esac
"#,
        ),
    )
    .unwrap();
    fs::set_permissions(&noctalia_path, fs::Permissions::from_mode(0o755)).unwrap();
    // Intervals: unlocked/connected, unlocked/disconnected, locked/connected, locked/disconnected.
    let noctalia =
        |scenario: &str, timeout_ms: u64, state: &str, lock_fails: bool, intervals: [u64; 4]| {
            fs::write(&hci_log, b"").unwrap();
            fs::write(&noctalia_log, b"").unwrap();
            fs::write(&noctalia_state, format!("{state}\n")).unwrap();
            provision_lock(&bus.directory);
            let mut command = Command::new(env!("CARGO_BIN_EXE_bluetooth-auth-noctalia-auto-lock"));
            command
                .args([
                    "--address-file",
                    phone.to_str().unwrap(),
                    "--timeout-ms",
                    &timeout_ms.to_string(),
                    "--unlocked-connected-interval-ms",
                    &intervals[0].to_string(),
                    "--unlocked-disconnected-interval-ms",
                    &intervals[1].to_string(),
                    "--locked-connected-interval-ms",
                    &intervals[2].to_string(),
                    "--locked-disconnected-interval-ms",
                    &intervals[3].to_string(),
                ])
                .env("DBUS_SYSTEM_BUS_ADDRESS", &bus.address)
                .env("LD_PRELOAD", &shim)
                .env("BT_AUTH_RUNTIME_DIR", &bus.directory)
                .env("BT_AUTH_HCI_SCENARIO", scenario)
                .env("BT_AUTH_HCI_LOG", &hci_log)
                .env("PATH", &bus.directory)
                .env("NOCTALIA_LOG", &noctalia_log)
                .env("NOCTALIA_STATE_FILE", &noctalia_state)
                .env("NOCTALIA_LOCK_FAIL", if lock_fails { "1" } else { "0" })
                .stdout(Stdio::piped())
                .stderr(Stdio::piped());
            command
        };
    let wait_for_status_calls = |expected: usize| {
        let deadline = Instant::now() + Duration::from_secs(3);
        loop {
            let log = fs::read_to_string(&noctalia_log).unwrap();
            if log.lines().filter(|line| *line == "status").count() >= expected {
                return log;
            }
            assert!(
                Instant::now() < deadline,
                "next status check did not arrive: {log:?}"
            );
            thread::sleep(Duration::from_millis(10));
        }
    };

    // A locked, connected session keeps querying but never attempts BLE or locks again.
    provision_lock(&bus.directory);
    let lock_path = bus.directory.join("hci0.lock");
    let held_lock = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(&lock_path)
        .unwrap();
    held_lock.try_lock().unwrap();
    fake.reset(Behavior::Accept);
    let mut command = noctalia(
        "encrypted",
        80,
        r#"{"locked":true}"#,
        false,
        [30_000, 30_000, 100, 30_000],
    );
    let child = BoundedChild::spawn(&mut command);
    assert!(!wait_for_status_calls(2).lines().any(|line| line == "lock"));
    assert!(fake.snapshot().methods.is_empty());
    assert_eq!(unsafe { libc::kill(child.id() as i32, libc::SIGTERM) }, 0);
    assert_eq!(
        child.wait(Duration::from_secs(2)).status.signal(),
        Some(libc::SIGTERM)
    );
    drop(held_lock);

    // Locked/disconnected attempts BLE again but never requests another session lock.
    fake.reset(Behavior::Accept);
    let mut command = noctalia(
        "unencrypted",
        80,
        r#"{"locked":true}"#,
        false,
        [30_000, 30_000, 30_000, 100],
    );
    let child = BoundedChild::spawn(&mut command);
    assert!(!wait_for_status_calls(2).lines().any(|line| line == "lock"));
    assert_eq!(unsafe { libc::kill(child.id() as i32, libc::SIGTERM) }, 0);
    assert_eq!(
        child.wait(Duration::from_secs(2)).status.signal(),
        Some(libc::SIGTERM)
    );
    let calls = fake.snapshot();
    assert!(!calls.gatt.is_empty());
    assert!(!calls.advertisements.is_empty());
    assert_no_forbidden_methods(&calls);
    assert_owners_gone(&bus, &calls);

    // After connecting, a second status call proves a new cycle began.
    // The other three intervals are 30 seconds, beyond the test deadline.
    fake.reset(Behavior::Accept);
    let mut command = noctalia(
        "delayed",
        250,
        r#"{"locked":false}"#,
        false,
        [100, 30_000, 30_000, 30_000],
    );
    let child = BoundedChild::spawn(&mut command);
    let calls = fake.wait_for_registrations();
    assert_owners_gone(&bus, &calls);
    assert!(!wait_for_status_calls(2).lines().any(|line| line == "lock"));
    assert_registration_counts(&calls, 1, 1);
    assert_no_forbidden_methods(&calls);
    assert_eq!(unsafe { libc::kill(child.id() as i32, libc::SIGTERM) }, 0);
    assert_eq!(
        child.wait(Duration::from_secs(2)).status.signal(),
        Some(libc::SIGTERM)
    );

    // A busy shared lock times out: lock once, then use the locked/disconnected interval.
    let held_lock = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(&lock_path)
        .unwrap();
    held_lock.try_lock().unwrap();
    fake.reset(Behavior::Accept);
    let mut command = noctalia(
        "disconnected",
        80,
        r#"{"locked":false}"#,
        false,
        [30_000, 30_000, 30_000, 100],
    );
    let child = BoundedChild::spawn(&mut command);
    // The second status call verifies locking; the third starts the next cycle.
    assert_eq!(
        wait_for_status_calls(3)
            .lines()
            .filter(|line| *line == "lock")
            .count(),
        1
    );
    assert!(fake.snapshot().methods.is_empty());
    assert_eq!(unsafe { libc::kill(child.id() as i32, libc::SIGTERM) }, 0);
    assert_eq!(
        child.wait(Duration::from_secs(2)).status.signal(),
        Some(libc::SIGTERM)
    );

    // An accepted lock request can leave the session unlocked: use the fourth interval.
    fake.reset(Behavior::Accept);
    let mut command = noctalia(
        "disconnected",
        80,
        r#"{"locked":false}"#,
        false,
        [30_000, 100, 30_000, 30_000],
    );
    command.env("NOCTALIA_LOCK_NOOP", "1");
    let child = BoundedChild::spawn(&mut command);
    wait_for_status_calls(3);
    assert!(fake.snapshot().methods.is_empty());
    assert_eq!(unsafe { libc::kill(child.id() as i32, libc::SIGTERM) }, 0);
    let output = child.wait(Duration::from_secs(2));
    assert_eq!(output.status.signal(), Some(libc::SIGTERM));
    assert!(
        String::from_utf8_lossy(&output.stderr)
            .contains("Noctalia still reports an unlocked session after 300 ms"),
        "{output:?}"
    );
    drop(held_lock);

    // Noctalia command failures still terminate the monitor.
    let held_lock = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(&lock_path)
        .unwrap();
    held_lock.try_lock().unwrap();
    fake.reset(Behavior::Accept);
    let mut command = noctalia("disconnected", 80, r#"{"locked":false}"#, true, [30_000; 4]);
    let output = BoundedChild::spawn(&mut command).wait(Duration::from_secs(2));
    assert_failed(&output, "Noctalia lock failure should propagate");
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("simulated lock failure"),
        "{output:?}"
    );
    assert_eq!(fs::read_to_string(&noctalia_log).unwrap(), "status\nlock\n");
}

#[test]
fn link_query_notify_and_sync_contracts() {
    let bus = PrivateBus::start();
    let shim = compile_shim(&bus.directory);
    let phone = bus.directory.join("phone");
    let log = bus.directory.join("hci.log");
    fs::write(&phone, format!("{TARGET}\n")).unwrap();
    let fake = FakeBluez::start(&bus.address);

    for (scenario, success) in [
        ("encrypted", true),
        ("disconnected", false),
        ("unencrypted", false),
        ("error", false),
    ] {
        fake.reset(Behavior::Accept);
        let mut command = link_command(&bus, &shim, &phone, &log, scenario, 0);
        let output = BoundedChild::spawn(&mut command).wait(Duration::from_secs(2));
        assert_eq!(output.status.success(), success, "{scenario}: {output:?}");
        assert_eq!(query_count(&log), 1, "{scenario}");
        assert!(fake.snapshot().methods.is_empty(), "{scenario}");
    }
    assert!(!bus.directory.join("prepare.log").exists());

    let connect_socket = bus.directory.join("connect.sock");
    let receiver = UnixDatagram::bind(&connect_socket).unwrap();
    receiver.set_nonblocking(true).unwrap();
    for (scenario, success) in [("encrypted", true), ("error", false)] {
        fake.reset(Behavior::Accept);
        let mut command = link_command(&bus, &shim, &phone, &log, scenario, -1);
        let output = BoundedChild::spawn(&mut command).wait(Duration::from_secs(2));
        assert_eq!(output.status.success(), success, "{scenario}: {output:?}");
        let mut no_packet = [0; 8];
        assert_eq!(
            receiver.recv(&mut no_packet).unwrap_err().kind(),
            std::io::ErrorKind::WouldBlock,
            "{scenario} query should not request connection"
        );
    }
    // -1 is the canonical value; any negative input has the same async behavior.
    for connect in [-1, -2, -1500, i64::MIN] {
        fake.reset(Behavior::Accept);
        let mut command = link_command(&bus, &shim, &phone, &log, "disconnected", connect);
        let output = BoundedChild::spawn(&mut command).wait(Duration::from_secs(2));
        assert_eq!(output.status.code(), Some(1), "{connect}: {output:?}");
        let mut payload = [0; 2];
        assert_eq!(receiver.recv(&mut payload).unwrap(), 1);
        assert_eq!(payload[0], 1);
        assert_eq!(query_count(&log), 1);
        assert!(fake.snapshot().methods.is_empty());
    }
    drop(receiver);
    assert!(!bus.directory.join("prepare.log").exists());

    // Waiting for a busy lock expires as a normal failure without registering anything.
    let lock_path = bus.directory.join("hci0.lock");
    let held_lock = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(&lock_path)
        .unwrap();
    held_lock.try_lock().unwrap();
    fake.reset(Behavior::Accept);
    let mut command = link_command(&bus, &shim, &phone, &log, "disconnected", 80);
    let started = Instant::now();
    let output = BoundedChild::spawn(&mut command).wait(Duration::from_secs(2));
    assert!(started.elapsed() >= Duration::from_millis(80));
    assert_eq!(output.status.code(), Some(1), "busy lock: {output:?}");
    assert!(
        output.stdout.is_empty() && output.stderr.is_empty(),
        "busy lock: {output:?}"
    );
    assert!(fake.snapshot().methods.is_empty());
    assert!(!bus.directory.join("prepare.log").exists());
    fake.reset(Behavior::Accept);
    let mut command = link_command(&bus, &shim, &phone, &log, "encrypted", 1000);
    let output = BoundedChild::spawn(&mut command).wait(Duration::from_secs(2));
    assert!(
        output.status.success(),
        "busy encrypted fast path: {output:?}"
    );
    assert_eq!(query_count(&log), 1);
    assert!(fake.snapshot().methods.is_empty());
    drop(held_lock);

    // A waiter's timeout does not interrupt the holder; terminating the holder
    // releases the lock for a later attempt.
    fake.reset(Behavior::Accept);
    let mut holder_command = link_command(&bus, &shim, &phone, &log, "unencrypted", 1000);
    let mut holder = BoundedChild::spawn(&mut holder_command);
    fake.wait_for_registrations();
    let mut command = link_command(&bus, &shim, &phone, &log, "disconnected", 80);
    let output = BoundedChild::spawn(&mut command).wait(Duration::from_secs(2));
    assert_eq!(output.status.code(), Some(1), "active holder: {output:?}");
    assert!(
        output.stdout.is_empty() && output.stderr.is_empty(),
        "active holder: {output:?}"
    );
    assert!(holder.0.as_mut().unwrap().try_wait().unwrap().is_none());
    assert_eq!(unsafe { libc::kill(holder.id() as i32, libc::SIGTERM) }, 0);
    let output = holder.wait(Duration::from_secs(2));
    assert_eq!(output.status.signal(), Some(libc::SIGTERM), "{output:?}");
    let calls = fake.snapshot();
    assert_registration_counts(&calls, 1, 1);
    assert_no_forbidden_methods(&calls);
    assert_owners_gone(&bus, &calls);

    fake.reset(Behavior::Accept);
    let mut command = link_command(&bus, &shim, &phone, &log, "unencrypted", 80);
    let output = BoundedChild::spawn(&mut command).wait(Duration::from_secs(2));
    assert_eq!(
        output.status.code(),
        Some(1),
        "connection was not established: {output:?}"
    );
    assert!(
        output.stderr.is_empty(),
        "a normal connection timeout must not report a runtime error: {output:?}"
    );
    let calls = fake.snapshot();
    assert_registration_counts(&calls, 1, 1);
    assert_no_forbidden_methods(&calls);
    assert_owners_gone(&bus, &calls);

    // Failed or stalled registrations release both D-Bus ownership and the lock.
    for (behavior, advertisements) in [
        (Behavior::RejectGatt, 0),
        (Behavior::RejectAdvertisement, 1),
        (Behavior::HangAdvertisement, 1),
    ] {
        fake.reset(behavior);
        let mut command = link_command(&bus, &shim, &phone, &log, "disconnected", 250);
        let output = BoundedChild::spawn(&mut command).wait(Duration::from_secs(2));
        if behavior == Behavior::HangAdvertisement {
            assert_eq!(output.status.code(), Some(1), "{output:?}");
            assert!(output.stderr.is_empty(), "normal timeout: {output:?}");
        } else {
            assert_failed(&output, "registration error should fail");
        }
        let calls = fake.snapshot();
        assert_registration_counts(&calls, 1, advertisements);
        assert_no_forbidden_methods(&calls);
        assert_owners_gone(&bus, &calls);
        let post_error_lock = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW)
            .open(&lock_path)
            .unwrap();
        post_error_lock.try_lock().unwrap();
    }

    fake.reset(Behavior::Accept);
    let ready = bus.directory.join("connected");
    let mut command = link_command(&bus, &shim, &phone, &log, "external", 1000);
    command.env("BT_AUTH_HCI_READY_FILE", &ready);
    let child = BoundedChild::spawn(&mut command);
    fake.wait_for_registrations();
    fs::write(&ready, b"").unwrap();
    let output = child.wait(Duration::from_secs(2));
    assert!(
        output.status.success(),
        "released lock should allow connection: {output:?}"
    );
    let calls = fake.snapshot();
    assert_registration_counts(&calls, 1, 1);
    assert_no_forbidden_methods(&calls);
    assert_owners_gone(&bus, &calls);
    let post_success_lock = fs::File::open(&lock_path).unwrap();
    post_success_lock.try_lock().unwrap();
}

#[test]
fn link_lock_wait_rechecks_connection_and_shares_deadline() {
    let bus = PrivateBus::start();
    let shim = compile_shim(&bus.directory);
    let phone = bus.directory.join("phone");
    let log = bus.directory.join("hci.log");
    fs::write(&phone, format!("{TARGET}\n")).unwrap();
    let fake = FakeBluez::start(&bus.address);
    provision_lock(&bus.directory);

    for connected in [true, false] {
        fake.reset(Behavior::Accept);
        let held_lock = fs::File::open(bus.directory.join("hci0.lock")).unwrap();
        held_lock.try_lock().unwrap();
        let ready = bus.directory.join(format!("connected-{connected}"));
        let mut command = link_command(&bus, &shim, &phone, &log, "external", 2000);
        command.env("BT_AUTH_HCI_READY_FILE", &ready);
        let mut child = BoundedChild::spawn(&mut command);
        let deadline = Instant::now() + Duration::from_secs(2);
        while query_count(&log) < 2 {
            assert!(Instant::now() < deadline, "initial queries did not arrive");
            thread::sleep(Duration::from_millis(10));
        }
        thread::sleep(Duration::from_millis(1200));
        assert!(child.0.as_mut().unwrap().try_wait().unwrap().is_none());
        assert!(fake.snapshot().methods.is_empty());
        assert!(!bus.directory.join("prepare.log").exists());
        if connected {
            fs::write(&ready, b"").unwrap();
        }
        drop(held_lock);

        // Only the remaining part of the 2-second budget is available after release.
        // Restarting that budget here would exceed this test deadline.
        let output = child.wait(Duration::from_millis(1300));
        assert_eq!(output.status.success(), connected, "{output:?}");
        let calls = fake.snapshot();
        if connected {
            // Reuse the connection established while waiting, with no HID registration.
            assert!(calls.methods.is_empty());
        } else {
            assert!(output.stderr.is_empty(), "normal timeout: {output:?}");
            assert_registration_counts(&calls, 1, 1);
            assert_no_forbidden_methods(&calls);
            assert_owners_gone(&bus, &calls);
        }
        let released_lock = fs::File::open(bus.directory.join("hci0.lock")).unwrap();
        released_lock.try_lock().unwrap();
    }
}

#[test]
fn preparation_failure_continues_and_timeout_reaps_under_the_lock() {
    let bus = PrivateBus::start();
    let shim = compile_shim(&bus.directory);
    let phone = bus.directory.join("phone");
    let log = bus.directory.join("hci.log");
    let ready = bus.directory.join("connected");
    let preparation_log = bus.directory.join("prepare.log");
    fs::write(&phone, format!("{TARGET}\n")).unwrap();
    let fake = FakeBluez::start(&bus.address);

    // A preparation error is diagnostic; encrypted LE established through HID still succeeds.
    let mut command = link_command(&bus, &shim, &phone, &log, "external", 1500);
    command
        .env("BT_AUTH_PREPARE_MODE", "fail")
        .env("BT_AUTH_HCI_READY_FILE", &ready);
    let child = BoundedChild::spawn(&mut command);
    fake.wait_for_registrations();
    fs::write(&ready, b"").unwrap();
    let output = child.wait(Duration::from_secs(2));
    assert!(output.status.success(), "{output:?}");
    assert_eq!(
        String::from_utf8_lossy(&output.stderr)
            .matches("simulated LE preparation failure")
            .count(),
        1
    );
    assert_eq!(query_count(&preparation_log), 1);
    assert_owners_gone(&bus, &fake.snapshot());
    fs::remove_file(&ready).unwrap();

    // A helper that consumes the budget is killed and reaped while the lock is held.
    fake.reset(Behavior::Accept);
    fs::write(&preparation_log, b"").unwrap();
    let mut command = link_command(&bus, &shim, &phone, &log, "disconnected", 700);
    command.env("BT_AUTH_PREPARE_MODE", "hang");
    let started = Instant::now();
    let child = BoundedChild::spawn(&mut command);
    while query_count(&preparation_log) == 0 {
        assert!(started.elapsed() < Duration::from_secs(2));
        thread::sleep(Duration::from_millis(5));
    }
    let helper_pid: u32 = fs::read_to_string(&preparation_log)
        .unwrap()
        .trim()
        .parse()
        .unwrap();
    let lock = fs::File::open(bus.directory.join("hci0.lock")).unwrap();
    assert!(matches!(lock.try_lock(), Err(fs::TryLockError::WouldBlock)));
    assert!(fake.snapshot().methods.is_empty());
    let output = child.wait(Duration::from_secs(2));
    assert_eq!(output.status.code(), Some(1), "{output:?}");
    assert!(started.elapsed() < Duration::from_millis(1400));
    assert!(
        !Path::new(&format!("/proc/{helper_pid}")).exists(),
        "preparation helper was not reaped"
    );
    lock.try_lock().unwrap();
    drop(lock);

    // Preparation and HID use the same budget; the latter does not start a fresh timeout.
    fake.reset(Behavior::Accept);
    let mut command = link_command(&bus, &shim, &phone, &log, "disconnected", 1000);
    command.env("BT_AUTH_PREPARE_MODE", "delay");
    let started = Instant::now();
    let output = BoundedChild::spawn(&mut command).wait(Duration::from_secs(2));
    assert_eq!(output.status.code(), Some(1), "{output:?}");
    assert!(started.elapsed() < Duration::from_millis(1400));
    assert_registration_counts(&fake.snapshot(), 1, 1);
    assert_owners_gone(&bus, &fake.snapshot());

    // A missing wrapper also leaves the HID path available.
    fake.reset(Behavior::Accept);
    fs::remove_file(bus.directory.join("prepare-le")).unwrap();
    let mut command = link_command(&bus, &shim, &phone, &log, "external", 1500);
    command.env("BT_AUTH_HCI_READY_FILE", &ready);
    let child = BoundedChild::spawn(&mut command);
    fake.wait_for_registrations();
    fs::write(&ready, b"").unwrap();
    let output = child.wait(Duration::from_secs(2));
    assert!(output.status.success(), "{output:?}");
    assert!(String::from_utf8_lossy(&output.stderr).contains("Cannot start LE preparation"));
    assert_owners_gone(&bus, &fake.snapshot());
}

#[test]
fn hid_server_exclusive_enrollment_restores_adapter_and_lock() {
    let bus = PrivateBus::start();
    let shim = compile_shim(&bus.directory);
    let fake = FakeBluez::start(&bus.address);
    let lock_path = bus.directory.join("hci0.lock");
    provision_lock(&bus.directory);
    let initial = fake.adapter_state();
    let server = || {
        let mut command = Command::new(env!("CARGO_BIN_EXE_bluetooth-auth-hid-server"));
        command
            .env("DBUS_SYSTEM_BUS_ADDRESS", &bus.address)
            .env("LD_PRELOAD", &shim)
            .env("BT_AUTH_RUNTIME_DIR", &bus.directory)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        command
    };

    // The server takes the exclusive lock before changing BlueZ. A contender
    // cannot register or change adapter state until the lock becomes available.
    let held_lock = fs::File::open(&lock_path).unwrap();
    held_lock.try_lock().unwrap();
    let mut command = server();
    let child = BoundedChild::spawn(&mut command);
    thread::sleep(Duration::from_millis(100));
    let calls = fake.snapshot();
    assert_registration_counts(&calls, 0, 0);
    assert!(!calls.methods.iter().any(|method| method == "Set"));
    assert_adapter_restored(&fake, &initial);
    drop(held_lock);
    let calls = fake.wait_for_registrations();
    assert!(advertisement_is_discoverable(&bus, &calls));
    assert_eq!(
        fake.adapter_state(),
        AdapterState {
            discoverable: false,
            connectable: false,
            pairable: true,
            pairable_timeout: 0,
        }
    );
    assert_eq!(unsafe { libc::kill(child.id() as i32, libc::SIGINT) }, 0);
    let output = child.wait(Duration::from_secs(2));
    assert!(output.status.success(), "{output:?}");
    let calls = fake.snapshot();
    assert_registration_counts(&calls, 1, 1);
    assert_hid_no_forbidden_methods(&calls);
    assert_owners_gone(&bus, &calls);
    assert_adapter_restored(&fake, &initial);
    let released_lock = fs::File::open(&lock_path).unwrap();
    released_lock.try_lock().unwrap();
    drop(released_lock);

    // Either registration rejection restores the adapter and releases the lock.
    for (behavior, advertisements) in [
        (Behavior::RejectGatt, 0),
        (Behavior::RejectAdvertisement, 1),
    ] {
        fake.reset(behavior);
        let mut command = server();
        let output = BoundedChild::spawn(&mut command).wait(Duration::from_secs(2));
        assert_failed(&output, "HID registration failure should propagate");
        let calls = fake.snapshot();
        assert_registration_counts(&calls, 1, advertisements);
        assert_hid_no_forbidden_methods(&calls);
        assert_owners_gone(&bus, &calls);
        assert_adapter_restored(&fake, &initial);
        let released_lock = fs::File::open(&lock_path).unwrap();
        released_lock.try_lock().unwrap();
        drop(released_lock);
    }

    // An interrupt while advertising is blocked follows the same cleanup path.
    fake.reset(Behavior::HangAdvertisement);
    let mut command = server();
    let mut child = BoundedChild::spawn(&mut command);
    fake.wait_for_registrations();
    assert!(child.0.as_mut().unwrap().try_wait().unwrap().is_none());
    assert_eq!(unsafe { libc::kill(child.id() as i32, libc::SIGTERM) }, 0);
    let output = child.wait(Duration::from_secs(2));
    assert!(output.status.success(), "{output:?}");
    let calls = fake.snapshot();
    assert_registration_counts(&calls, 1, 1);
    assert_hid_no_forbidden_methods(&calls);
    assert_owners_gone(&bus, &calls);
    assert_adapter_restored(&fake, &initial);
    let released_lock = fs::File::open(&lock_path).unwrap();
    released_lock.try_lock().unwrap();
}
