#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <unistd.h>

#define HCIGETCONNLIST _IOR('H', 212, int)

static int bluetooth_fd = -1;
static unsigned int query_count = 0;

static void log_query(unsigned int count) {
    const char *path = getenv("BT_AUTH_HCI_LOG");
    char line[32];
    int fd;
    int length;

    if (path == NULL)
        return;
    fd = open(path, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0600);
    if (fd < 0)
        return;
    length = snprintf(line, sizeof(line), "%u\n", count);
    if (length > 0)
        (void)write(fd, line, (size_t)length);
    (void)close(fd);
}

int socket(int domain, int type, int protocol) {
    if (domain != AF_BLUETOOTH)
        return (int)syscall(SYS_socket, domain, type, protocol);

    /* Never let any Bluetooth socket escape to the host kernel. */
    if (type != (SOCK_RAW | SOCK_CLOEXEC) || protocol != 1) {
        errno = EAFNOSUPPORT;
        return -1;
    }
    bluetooth_fd = open("/dev/null", O_RDONLY | O_CLOEXEC);
    return bluetooth_fd;
}

int close(int fd) {
    if (fd == bluetooth_fd)
        bluetooth_fd = -1;
    return (int)syscall(SYS_close, fd);
}

int ioctl(int fd, unsigned long request, ...) {
    const char *scenario;
    unsigned char *buffer;
    unsigned char entry[16] = {0};
    uint16_t adapter;
    uint16_t capacity;
    uint16_t count;
    uint16_t connected = 1;
    uint32_t mode = 0;
    va_list args;

    va_start(args, request);
    buffer = va_arg(args, unsigned char *);
    va_end(args);

    if (fd != bluetooth_fd)
        return (int)syscall(SYS_ioctl, fd, request, buffer);
    if (request != HCIGETCONNLIST) {
        errno = ENOTTY;
        return -1;
    }

    query_count++;
    log_query(query_count);
    scenario = getenv("BT_AUTH_HCI_SCENARIO");
    if (scenario == NULL || strcmp(scenario, "error") == 0 ||
        (strcmp(scenario, "late-error") == 0 && query_count >= 2)) {
        errno = EIO;
        return -1;
    }

    memcpy(&adapter, buffer, sizeof(adapter));
    memcpy(&capacity, buffer + 2, sizeof(capacity));
    if (adapter != 0 || capacity != 512) {
        errno = EINVAL;
        return -1;
    }

    if (strcmp(scenario, "disconnected") == 0 ||
        strcmp(scenario, "late-error") == 0) {
        count = 0;
        memcpy(buffer + 2, &count, sizeof(count));
        return 0;
    }

    if (strcmp(scenario, "encrypted") != 0 &&
        strcmp(scenario, "delayed") != 0 &&
        strcmp(scenario, "unencrypted") != 0) {
        errno = EINVAL;
        return -1;
    }

    count = 1;
    memcpy(buffer + 2, &count, sizeof(count));
    /* Linux bdaddr_t order for 02:00:00:00:00:01. */
    entry[2] = 0x01;
    entry[7] = 0x02;
    entry[8] = 0x80; /* LE_LINK */
    memcpy(entry + 10, &connected, sizeof(connected));
    if (strcmp(scenario, "encrypted") == 0 ||
        (strcmp(scenario, "delayed") == 0 && query_count >= 3))
        mode = 0x0004; /* HCI_LM_ENCRYPT */
    memcpy(entry + 12, &mode, sizeof(mode));
    memcpy(buffer + 4, entry, sizeof(entry));
    return 0;
}
