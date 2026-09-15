#!/usr/bin/env python3
"""Exercise a generated PAM stack in a private configuration directory."""

import ctypes
import pathlib
import sys


PAM_SUCCESS = 0
PAM_ESTABLISH_CRED = 2
PAM_RUSER = 8


class PamConv(ctypes.Structure):
    _fields_ = [("conv", ctypes.c_void_p), ("appdata_ptr", ctypes.c_void_p)]


def require(result: bool, message: str) -> None:
    if not result:
        raise AssertionError(message)


def authenticate(
    libpam,
    confdir: pathlib.Path,
    service: str,
    user: str,
    ruser: str | None,
    ble_result: int,
    next_result: int,
    expect_success: bool,
    expect_ble: bool,
    expect_next: bool,
) -> None:
    state_dir = confdir / "state"
    state_dir.mkdir(exist_ok=True)
    ble_mark = state_dir / f"{service}-{user}-ble"
    next_mark = state_dir / f"{service}-{user}-next"
    for path in (ble_mark, next_mark):
        path.unlink(missing_ok=True)

    handle = ctypes.c_void_p()
    conversation = PamConv(None, None)
    result = libpam.pam_start_confdir(
        service.encode(),
        user.encode(),
        ctypes.byref(conversation),
        str(confdir).encode(),
        ctypes.byref(handle),
    )
    require(result == PAM_SUCCESS, f"pam_start_confdir({service}): {result}")
    try:
        if ruser is not None:
            result = libpam.pam_set_item(handle, PAM_RUSER, ruser.encode())
            require(result == PAM_SUCCESS, f"pam_set_item(PAM_RUSER): {result}")
        for key, value in {
            "BLE_RESULT": str(ble_result),
            "NEXT_RESULT": str(next_result),
            "BLE_MARK": str(ble_mark),
            "NEXT_MARK": str(next_mark),
        }.items():
            result = libpam.pam_putenv(handle, f"{key}={value}".encode())
            require(result == PAM_SUCCESS, f"pam_putenv({key}): {result}")
        result = libpam.pam_authenticate(handle, 0)
        require(
            (result == PAM_SUCCESS) == expect_success,
            f"{service}/{user}/{ruser}: PAM result {result}",
        )
        require(
            ble_mark.exists() == expect_ble,
            f"{service}/{user}/{ruser}: BLE invocation mismatch",
        )
        require(
            next_mark.exists() == expect_next,
            f"{service}/{user}/{ruser}: fallback invocation mismatch",
        )
    finally:
        libpam.pam_end(handle, result)


def establish_credentials(
    libpam, confdir: pathlib.Path, service: str, user: str
) -> None:
    """Exercise session setup without a preceding pam_authenticate call."""
    ble_mark = confdir / f"{service}-setcred-ble"
    handle = ctypes.c_void_p()
    conversation = PamConv(None, None)
    result = libpam.pam_start_confdir(
        service.encode(),
        user.encode(),
        ctypes.byref(conversation),
        str(confdir).encode(),
        ctypes.byref(handle),
    )
    require(result == PAM_SUCCESS, f"pam_start_confdir({service}): {result}")
    try:
        result = libpam.pam_putenv(handle, f"BLE_MARK={ble_mark}".encode())
        require(result == PAM_SUCCESS, f"pam_putenv(BLE_MARK): {result}")
        result = libpam.pam_acct_mgmt(handle, 0)
        require(result == PAM_SUCCESS, f"pam_acct_mgmt({service}): {result}")
        result = libpam.pam_setcred(handle, PAM_ESTABLISH_CRED)
        require(result == PAM_SUCCESS, f"pam_setcred({service}): {result}")
        require(not ble_mark.exists(), f"{service}: setcred ran the Bluetooth helper")
    finally:
        libpam.pam_end(handle, result)


def main() -> None:
    confdir = pathlib.Path(sys.argv[1])
    libpam = ctypes.CDLL(sys.argv[2])
    libpam.pam_start_confdir.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.POINTER(PamConv),
        ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    libpam.pam_set_item.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    libpam.pam_putenv.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    libpam.pam_authenticate.argtypes = [ctypes.c_void_p, ctypes.c_int]
    libpam.pam_acct_mgmt.argtypes = [ctypes.c_void_p, ctypes.c_int]
    libpam.pam_setcred.argtypes = [ctypes.c_void_p, ctypes.c_int]
    libpam.pam_end.argtypes = [ctypes.c_void_p, ctypes.c_int]

    # greetd starts its greeter without authenticating, using a login substack.
    establish_credentials(libpam, confdir, "greeter", "greeter")
    # Credential setup must also tolerate sudo's separate PAM_RUSER guard.
    establish_credentials(libpam, confdir, "sudo-credentials", "root")

    # Trusted PAM_USER and sudo's trusted PAM_RUSER both may take the BLE success path.
    authenticate(libpam, confdir, "sudo", "nobody", None, 0, 1, True, True, False)
    authenticate(libpam, confdir, "sudo", "root", "nobody", 0, 1, True, True, False)
    # A different user never runs the Bluetooth helper and still reaches password fallback.
    authenticate(libpam, confdir, "sudo", "root", "root", 0, 0, True, False, True)
    # Locker eligibility intentionally ignores PAM_RUSER and checks PAM_USER only.
    authenticate(libpam, confdir, "locker", "root", "nobody", 0, 0, True, False, True)
    # A missing or failed BLE check falls through to the next PAM rule.
    authenticate(libpam, confdir, "sudo", "nobody", None, 1, 0, True, True, True)
    authenticate(libpam, confdir, "sudo", "nobody", None, 2, 0, True, True, True)
    authenticate(libpam, confdir, "sudo", "nobody", None, 1, 1, False, True, True)
    authenticate(libpam, confdir, "locker", "nobody", None, 0, 1, True, True, False)
    # greetd only trusts PAM_USER.  PAM_RUSER cannot make another login eligible.
    authenticate(libpam, confdir, "greetd", "nobody", None, 0, 1, True, True, False)
    authenticate(libpam, confdir, "greetd", "nobody", "root", 1, 0, True, True, True)
    authenticate(libpam, confdir, "greetd", "nobody", None, 2, 0, True, True, True)
    authenticate(libpam, confdir, "greetd", "nobody", None, 1, 1, False, True, True)
    authenticate(libpam, confdir, "greetd", "root", "nobody", 0, 0, True, False, True)
    # A prior required failure remains fatal even if BLE subsequently succeeds.
    authenticate(
        libpam, confdir, "prior-failure", "nobody", None, 0, 0, False, True, True
    )


if __name__ == "__main__":
    main()
