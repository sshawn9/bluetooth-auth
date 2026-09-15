//! Exercise real MGMT packet I/O over a Unix socket pair; never open Bluetooth sockets.
#![cfg(target_os = "linux")]

use std::{
    io,
    os::{
        fd::{AsRawFd, IntoRawFd},
        unix::net::UnixDatagram,
    },
    sync::{Mutex, mpsc},
    thread,
    time::Duration,
};

use bluetooth_auth::{disconnect_classic, remove_classic_pairing};

const TARGET: bluer::Address = bluer::Address([2, 0x10, 0x20, 0x30, 0x40, 0x50]);
const WIRE_ADDRESS: [u8; 7] = [0x50, 0x40, 0x30, 0x20, 0x10, 2, 0];

struct Mock {
    socket: Option<UnixDatagram>,
    fd: i32,
    socket_calls: usize,
    bound: bool,
    deny_bind: bool,
}

static MOCK: Mutex<Option<Mock>> = Mutex::new(None);

#[unsafe(export_name = "socket")]
unsafe extern "C" fn mock_socket(domain: i32, kind: i32, protocol: i32) -> i32 {
    if domain != libc::AF_BLUETOOTH {
        return unsafe { libc::syscall(libc::SYS_socket, domain, kind, protocol) as i32 };
    }
    let mut state = MOCK.lock().unwrap();
    if let Some(state) = state.as_mut() {
        state.socket_calls += 1;
        if kind == libc::SOCK_RAW | libc::SOCK_CLOEXEC | libc::SOCK_NONBLOCK
            && protocol == 1
            && let Some(socket) = state.socket.take()
        {
            state.fd = socket.into_raw_fd();
            return state.fd;
        }
    }
    unsafe { *libc::__errno_location() = libc::EACCES };
    -1
}

#[unsafe(export_name = "bind")]
unsafe extern "C" fn mock_bind(
    fd: i32,
    address: *const libc::sockaddr,
    length: libc::socklen_t,
) -> i32 {
    let mut state = MOCK.lock().unwrap();
    if let Some(state) = state.as_mut()
        && fd == state.fd
    {
        if length == 6 && !address.is_null() && !state.deny_bind {
            // Native sockaddr_hci contains three u16 fields.
            let bytes = unsafe { std::slice::from_raw_parts(address.cast::<u8>(), 6) };
            let fields: Vec<_> = bytes
                .as_chunks::<2>()
                .0
                .iter()
                .map(|b| u16::from_ne_bytes([b[0], b[1]]))
                .collect();
            if fields == [libc::AF_BLUETOOTH as u16, 0xffff, 3] {
                state.bound = true;
                return 0;
            }
        }
        unsafe { *libc::__errno_location() = libc::EACCES };
        return -1;
    }
    unsafe { libc::syscall(libc::SYS_bind, fd, address, length) as i32 }
}

fn response(event: u16, opcode: u16, status: u8, payload: &[u8]) -> Vec<u8> {
    let mut bytes = Vec::new();
    bytes.extend(event.to_le_bytes());
    bytes.extend([0, 0]); // controller hci0
    bytes.extend(((3 + payload.len()) as u16).to_le_bytes());
    bytes.extend(opcode.to_le_bytes());
    bytes.push(status);
    bytes.extend(payload);
    bytes
}

fn exercise(opcode: u16, replies: Vec<Vec<u8>>, success: bool) {
    let (socket, peer) = UnixDatagram::pair().unwrap();
    socket.set_nonblocking(true).unwrap();
    peer.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
    let fd = socket.as_raw_fd();
    *MOCK.lock().unwrap() = Some(Mock {
        socket: Some(socket),
        fd: -1,
        socket_calls: 0,
        bound: false,
        deny_bind: false,
    });
    let (finished_tx, finished_rx) = mpsc::channel();
    let server = thread::spawn(move || {
        let mut bytes = [0; 64];
        let size = peer.recv(&mut bytes).unwrap();
        let request = bytes[..size].to_vec();
        for reply in replies {
            peer.send(&reply).unwrap();
        }
        finished_rx.recv_timeout(Duration::from_secs(7)).unwrap();
        peer.set_nonblocking(true).unwrap();
        assert_eq!(
            peer.recv(&mut bytes).unwrap_err().kind(),
            io::ErrorKind::WouldBlock,
            "must not send a second command"
        );
        request
    });

    let result = match opcode {
        0x0014 => disconnect_classic(TARGET),
        0x001b => remove_classic_pairing(TARGET),
        _ => unreachable!(),
    };
    assert_eq!(result.is_ok(), success, "unexpected result: {result:?}");
    assert_eq!(
        unsafe { libc::fcntl(fd, libc::F_GETFD) },
        -1,
        "socket must be closed"
    );
    finished_tx.send(()).unwrap();
    let request = server.join().unwrap();
    let expected = match opcode {
        0x0014 => vec![0x14, 0, 0, 0, 7, 0, 0x50, 0x40, 0x30, 0x20, 0x10, 2, 0],
        // Address type=0; Disconnect=0. LE is neither unpaired nor disconnected.
        _ => vec![0x1b, 0, 0, 0, 8, 0, 0x50, 0x40, 0x30, 0x20, 0x10, 2, 0, 0],
    };
    assert_eq!(request, expected);
    let state = MOCK.lock().unwrap().take().unwrap();
    assert_eq!(state.socket_calls, 1);
    assert!(state.bound);
}

#[test]
fn classic_operations_are_scoped_idempotent_and_bounded() {
    for (opcode, absent, wrong_absent) in [(0x0014, 0x02, 0x06), (0x001b, 0x06, 0x02)] {
        exercise(opcode, vec![response(1, opcode, 0, &WIRE_ADDRESS)], true);
        // Both status encodings can report an already-satisfied operation.
        exercise(
            opcode,
            vec![response(1, opcode, absent, &WIRE_ADDRESS)],
            true,
        );
        exercise(opcode, vec![response(2, opcode, absent, &[])], true);
        exercise(opcode, vec![response(2, opcode, wrong_absent, &[])], false);
        exercise(
            opcode,
            vec![response(1, opcode, 0x0f, &WIRE_ADDRESS)],
            false,
        ); // Not Powered

        let mut other_controller = response(1, opcode, 0x0a, &WIRE_ADDRESS);
        other_controller[2] = 1;
        exercise(
            opcode,
            vec![
                vec![0x04, 0, 0, 0, 0, 0], // unrelated controller event
                other_controller,
                response(1, 0xffff, 0x0a, &WIRE_ADDRESS),
                response(2, opcode, 0, &[]),
                response(1, opcode, 0, &WIRE_ADDRESS),
            ],
            true,
        );
        exercise(
            opcode,
            vec![
                response(2, opcode, 0, &[]),
                response(1, opcode, 0x0a, &WIRE_ADDRESS),
            ],
            false,
        );

        let mut wrong_target = WIRE_ADDRESS;
        wrong_target[0] ^= 1;
        exercise(opcode, vec![response(1, opcode, 0, &wrong_target)], false);
        let mut wrong_transport = WIRE_ADDRESS;
        wrong_transport[6] = 1;
        exercise(
            opcode,
            vec![response(1, opcode, 0, &wrong_transport)],
            false,
        );
        let mut truncated = response(1, opcode, 0, &WIRE_ADDRESS);
        truncated.pop();
        exercise(opcode, vec![truncated], false);
    }
    // A lone successful Command Status is not completion; the wait must end.
    exercise(0x0014, vec![response(2, 0x0014, 0, &[])], false);

    assert!(disconnect_classic(TARGET).is_err()); // socket permission failure
    assert!(remove_classic_pairing(TARGET).is_err());
    let (socket, _peer) = UnixDatagram::pair().unwrap();
    let fd = socket.as_raw_fd();
    *MOCK.lock().unwrap() = Some(Mock {
        socket: Some(socket),
        fd: -1,
        socket_calls: 0,
        bound: false,
        deny_bind: true,
    });
    assert!(disconnect_classic(TARGET).is_err());
    assert_eq!(unsafe { libc::fcntl(fd, libc::F_GETFD) }, -1);
    *MOCK.lock().unwrap() = None;
}
