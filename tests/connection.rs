//! 通过替换 socket/ioctl 符号注入连接快照，绝不访问真实蓝牙。
#![cfg(target_os = "linux")]

use std::{fs::File, os::fd::IntoRawFd, sync::Mutex};

const REQUEST: libc::Ioctl = libc::_IOR::<libc::c_int>(b'H' as u32, 212);
const TARGET: [u8; 6] = [0x02, 0, 0, 0, 0, 1];

#[derive(Default)]
struct Mock {
    data: Vec<u8>,
    socket_error: bool,
    ioctl_error: bool,
    socket_calls: usize,
    ioctl_calls: usize,
    fd: Option<i32>,
}

static MOCK: Mutex<Mock> = Mutex::new(Mock {
    data: Vec::new(),
    socket_error: false,
    ioctl_error: false,
    socket_calls: 0,
    ioctl_calls: 0,
    fd: None,
});

#[unsafe(export_name = "socket")]
unsafe extern "C" fn mock_socket(domain: i32, kind: i32, protocol: i32) -> i32 {
    let mut mock = MOCK.lock().unwrap();
    mock.socket_calls += 1;
    if domain != libc::AF_BLUETOOTH
        || kind != libc::SOCK_RAW | libc::SOCK_CLOEXEC
        || protocol != 1
        || mock.socket_error
    {
        unsafe { *libc::__errno_location() = libc::EACCES };
        return -1;
    }
    // 用普通文件代替 socket，OwnedFd 的关闭仍走真实操作系统接口。
    let fd = File::open("/dev/null").unwrap().into_raw_fd();
    mock.fd = Some(fd);
    fd
}

#[unsafe(export_name = "ioctl")]
unsafe extern "C" fn mock_ioctl(fd: i32, request: libc::Ioctl, data: *mut libc::c_void) -> i32 {
    let mut mock = MOCK.lock().unwrap();
    mock.ioctl_calls += 1;
    if request != REQUEST || mock.fd != Some(fd) || mock.ioctl_error {
        unsafe { *libc::__errno_location() = libc::EIO };
        return -1;
    }
    let header = unsafe { std::slice::from_raw_parts(data.cast::<u8>(), 4) };
    if u16::from_ne_bytes([header[0], header[1]]) != 0
        || u16::from_ne_bytes([header[2], header[3]]) != 512
        || mock.data.len() > 4 + 512 * 16
    {
        unsafe { *libc::__errno_location() = libc::EINVAL };
        return -1;
    }
    unsafe { std::ptr::copy_nonoverlapping(mock.data.as_ptr(), data.cast(), mock.data.len()) };
    0
}

fn entry(mut address: [u8; 6], transport: u8, state: u16, mode: u32) -> [u8; 16] {
    address.reverse();
    let mut bytes = [0; 16];
    bytes[2..8].copy_from_slice(&address);
    bytes[8] = transport;
    bytes[10..12].copy_from_slice(&state.to_ne_bytes());
    bytes[12..16].copy_from_slice(&mode.to_ne_bytes());
    bytes
}

fn snapshot(entries: &[[u8; 16]]) -> Vec<u8> {
    let mut data = vec![0; 4];
    data[2..4].copy_from_slice(&(entries.len() as u16).to_ne_bytes());
    for entry in entries {
        data.extend_from_slice(entry);
    }
    data
}

fn check(mock: Mock, target: [u8; 6], expected: Option<bool>) {
    *MOCK.lock().unwrap() = mock;
    let result = bluetooth_auth::query_connection(bluer::Address(target));
    match expected {
        Some(expected) => assert_eq!(result.unwrap(), expected),
        None => assert!(result.is_err()),
    }
    let mock = MOCK.lock().unwrap();
    assert_eq!(mock.socket_calls, 1, "查询不得重试创建 socket");
    assert_eq!(mock.ioctl_calls, usize::from(!mock.socket_error));
    if let Some(fd) = mock.fd {
        assert_eq!(unsafe { libc::fcntl(fd, libc::F_GETFD) }, -1);
        assert_eq!(
            std::io::Error::last_os_error().raw_os_error(),
            Some(libc::EBADF)
        );
    }
}

#[test]
fn connection_snapshot() {
    let encrypted = entry(TARGET, 0x80, 1, 0x0004);
    let classic = entry(TARGET, 0x01, 1, 0x0004);
    let other_address = [0x02, 0, 0, 0, 0, 2];
    let other = entry(other_address, 0x80, 1, 0x0004);
    for (entries, expected) in [
        (vec![], Some(false)),
        (vec![encrypted], Some(true)),
        (vec![entry(TARGET, 0x80, 1, 0)], Some(false)),
        (vec![entry(TARGET, 0x80, 2, 0x0004)], Some(false)),
        (vec![classic], Some(false)),
        (vec![other], Some(false)),
        (vec![classic, other], Some(false)),
        (vec![classic, other, encrypted], Some(true)),
        (vec![encrypted, encrypted], None),
    ] {
        check(
            Mock {
                data: snapshot(&entries),
                ..Default::default()
            },
            TARGET,
            expected,
        );
    }
    check(
        Mock {
            data: snapshot(&[other]),
            ..Default::default()
        },
        other_address,
        Some(true),
    );
    let mut full = snapshot(&[]);
    full[2..4].copy_from_slice(&512u16.to_ne_bytes());
    check(
        Mock {
            data: full,
            ..Default::default()
        },
        TARGET,
        None,
    );
    let mut wrong_adapter = snapshot(&[]);
    wrong_adapter[0..2].copy_from_slice(&1u16.to_ne_bytes());
    check(
        Mock {
            data: wrong_adapter,
            ..Default::default()
        },
        TARGET,
        None,
    );
    check(
        Mock {
            socket_error: true,
            ..Default::default()
        },
        TARGET,
        None,
    );
    check(
        Mock {
            ioctl_error: true,
            ..Default::default()
        },
        TARGET,
        None,
    );
}
