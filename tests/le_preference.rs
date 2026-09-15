//! LE preparation uses a private D-Bus and a local MGMT socket stand-in.
#![cfg(target_os = "linux")]

use std::{
    io,
    os::{fd::IntoRawFd, unix::net::UnixDatagram},
    process::Command,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    thread,
    time::Duration,
};

use bluer::Address;
use dbus::{blocking::Connection, channel::MatchingReceiver};
use dbus_crossroads::Crossroads;

mod support;
use support::PrivateBus;

struct HciMock {
    sockets: Vec<UnixDatagram>,
    fd: i32,
    socket_calls: usize,
}

static HCI: Mutex<Option<HciMock>> = Mutex::new(None);

#[unsafe(export_name = "socket")]
unsafe extern "C" fn reject_bluetooth_socket(domain: i32, kind: i32, protocol: i32) -> i32 {
    if domain == libc::AF_BLUETOOTH {
        let mut state = HCI.lock().unwrap();
        if let Some(state) = state.as_mut() {
            state.socket_calls += 1;
            if kind == libc::SOCK_RAW | libc::SOCK_CLOEXEC | libc::SOCK_NONBLOCK
                && protocol == 1
                && let Some(socket) = state.sockets.pop()
            {
                state.fd = socket.into_raw_fd();
                return state.fd;
            }
        }
        unsafe { *libc::__errno_location() = libc::EPERM };
        -1
    } else {
        unsafe { libc::syscall(libc::SYS_socket, domain, kind, protocol) as i32 }
    }
}

#[unsafe(export_name = "bind")]
unsafe extern "C" fn mock_hci_bind(
    fd: i32,
    address: *const libc::sockaddr,
    length: libc::socklen_t,
) -> i32 {
    if HCI
        .lock()
        .unwrap()
        .as_ref()
        .is_some_and(|state| state.fd == fd)
    {
        assert_eq!(length, 6);
        assert!(!address.is_null());
        return 0;
    }
    unsafe { libc::syscall(libc::SYS_bind, fd, address, length) as i32 }
}

#[derive(Clone, Copy)]
enum Outcome {
    Ok,
    Missing,
    DifferentPath,
    Unknown,
    Denied,
}

struct FakeBluez {
    values: Arc<Mutex<Vec<String>>>,
    stop: Arc<AtomicBool>,
    thread: thread::JoinHandle<()>,
}

impl FakeBluez {
    fn start(outcome: Outcome, target: Address) -> Self {
        let values = Arc::new(Mutex::new(Vec::new()));
        let stop = Arc::new(AtomicBool::new(false));
        let (ready_tx, ready_rx) = mpsc::channel();
        let thread_values = values.clone();
        let thread_stop = stop.clone();
        let path_address = if matches!(outcome, Outcome::Missing) {
            Address([2, 0, 0, 0, 0, 2]) // Only another device exists.
        } else {
            target
        };
        let mut path = format!(
            "/org/bluez/hci0/dev_{}",
            path_address.to_string().replace(':', "_")
        );
        if matches!(outcome, Outcome::DifferentPath) {
            path = "/org/bluez/hci0/device_discovered_before_identity_resolution".into();
        }

        let thread = thread::spawn(move || {
            let connection = Connection::new_system().unwrap();
            connection
                .request_name("org.bluez", false, true, false)
                .unwrap();
            let mut crossroads = Crossroads::new();
            let device = crossroads.register("org.bluez.Device1", move |builder| {
                builder
                    .property::<String, _>("Address")
                    .get(move |_, _: &mut Arc<Mutex<Vec<String>>>| Ok(path_address.to_string()));
                if matches!(outcome, Outcome::Unknown) {
                    return;
                }
                builder.property::<String, _>("PreferredBearer").set(
                    move |_, data: &mut Arc<Mutex<Vec<String>>>, value| {
                        if matches!(outcome, Outcome::Denied) {
                            return Err(dbus::MethodErr::failed("permission denied"));
                        }
                        data.lock().unwrap().push(value.clone());
                        Ok(Some(value))
                    },
                );
            });
            crossroads.insert("/", &[crossroads.object_manager()], ());
            crossroads.insert(path, &[device], thread_values);
            connection.start_receive(
                dbus::message::MatchRule::new_method_call(),
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
            values,
            stop,
            thread,
        }
    }

    fn finish(self) -> Vec<String> {
        self.stop.store(true, Ordering::Release);
        self.thread.join().unwrap();
        self.values.lock().unwrap().clone()
    }
}

fn exercise(outcome: Outcome, expected_ok: bool) {
    let target = Address([2, 0, 0, 0, 0, 1]);
    let fake = FakeBluez::start(outcome, target);
    let result = bluetooth_auth::prefer_le(target);
    assert_eq!(result.is_ok(), expected_ok);
    if expected_ok {
        bluetooth_auth::prefer_le(target).unwrap();
    }
    let values = fake.finish();
    if matches!(outcome, Outcome::Ok | Outcome::DifferentPath) {
        assert_eq!(values, ["le", "le"]);
    } else {
        assert!(values.is_empty());
    }
}

fn management_reply(opcode: u16, status: u8) -> Vec<u8> {
    let mut reply = Vec::from([1, 0, 0, 0, 10, 0]); // Command Complete / hci0 / payload length.
    reply.extend(opcode.to_le_bytes());
    reply.push(status);
    reply.extend([1, 0, 0, 0, 0, 2, 0]); // Target in Linux bdaddr_t order, BR/EDR.
    reply
}

fn prepare(outcome: Outcome, replies: &[(u16, u8)], expected: bool) {
    let target = Address([2, 0, 0, 0, 0, 1]);
    let fake = FakeBluez::start(outcome, target);
    let mut sockets = Vec::new();
    let mut peers = Vec::new();
    for _ in replies {
        let (socket, peer) = UnixDatagram::pair().unwrap();
        peer.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
        sockets.push(socket);
        peers.push(peer);
    }
    *HCI.lock().unwrap() = Some(HciMock {
        sockets,
        fd: -1,
        socket_calls: 0,
    });
    let replies = replies.to_vec();
    let expected_socket_calls = replies.len();
    let (complete_tx, complete_rx) = mpsc::channel();
    let server = thread::spawn(move || {
        let mut used_peers = Vec::new();
        for (opcode, status) in replies {
            let peer = peers.pop().unwrap();
            let mut request = [0; 32];
            let size = peer.recv(&mut request).unwrap();
            assert!(size >= 2);
            assert_eq!(u16::from_le_bytes([request[0], request[1]]), opcode);
            peer.send(&management_reply(opcode, status)).unwrap();
            used_peers.push(peer);
        }
        complete_rx.recv_timeout(Duration::from_secs(2)).unwrap();
        for peer in used_peers {
            let mut request = [0; 32];
            peer.set_nonblocking(true).unwrap();
            assert_eq!(
                peer.recv(&mut request).unwrap_err().kind(),
                io::ErrorKind::WouldBlock
            );
        }
    });

    assert_eq!(bluetooth_auth::prepare_le(target), expected);
    complete_tx.send(()).unwrap();
    server.join().unwrap();
    let state = HCI.lock().unwrap().take().unwrap();
    assert_eq!(state.socket_calls, expected_socket_calls);
    let values = fake.finish();
    if matches!(outcome, Outcome::Ok) {
        assert_eq!(values, ["le"]);
    } else {
        assert!(values.is_empty());
    }
}

fn child() {
    exercise(Outcome::Ok, true);
    exercise(Outcome::Missing, true);
    exercise(Outcome::DifferentPath, true);
    exercise(Outcome::Unknown, false);
    exercise(Outcome::Denied, false);
    assert!(bluetooth_auth::prefer_le(Address([2, 0, 0, 0, 0, 1])).is_err()); // BlueZ unavailable

    // A preference error is reported but does not prevent cleanup.
    prepare(Outcome::Denied, &[(0x0014, 0), (0x001b, 0)], false);
    // A failed or uncertain disconnect makes unpairing unsafe.
    prepare(Outcome::Ok, &[(0x0014, 0x0f)], false);
    // Once disconnect completes, an unpair error still reaches the caller.
    prepare(Outcome::Ok, &[(0x0014, 0), (0x001b, 0x0f)], false);
    // Already-disconnected and already-unpaired are successful no-ops.
    prepare(Outcome::Ok, &[(0x0014, 0x02), (0x001b, 0x06)], true);
}

#[test]
fn prefer_le_and_prepare_le_follow_their_error_policies() {
    if std::env::var_os("BT_AUTH_LE_PREFERENCE_CHILD").is_some() {
        child();
        return;
    }

    let bus = PrivateBus::start();
    let output = Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "prefer_le_and_prepare_le_follow_their_error_policies",
            "--nocapture",
        ])
        .env("BT_AUTH_LE_PREFERENCE_CHILD", "1")
        .env("DBUS_SYSTEM_BUS_ADDRESS", &bus.address)
        .output()
        .unwrap();

    assert!(
        output.status.success(),
        "{}
{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
}
