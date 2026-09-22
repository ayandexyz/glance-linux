/*
 * pam_glance — ask the user's glanced daemon for a face-unlock verdict.
 *
 * The module does no vision work. It connects to the daemon's auth socket in
 * the user's runtime directory, sends one `authenticate` request, and maps
 * the answer onto PAM_SUCCESS / PAM_AUTH_ERR. Everything that decides is in
 * the daemon (see src/glanced/daemon.py); this file is deliberately dumb.
 *
 * Safety rails, because a PAM module that trusts a user-owned socket is a
 * privilege escalation the moment it is used for anything but the user's
 * own lock screen:
 *
 *   - refuses unless the process's real uid IS the account being
 *     authenticated (a lock screen runs as you; a login manager does not);
 *   - refuses in any setuid context (euid != uid), which is what `sudo` and
 *     `su` are — so listing it in their stacks does nothing;
 *   - refuses if the socket is not owned by that same uid.
 *
 * Use it only in the lock screen's PAM service. See pam/README.md.
 */

#define PAM_SM_AUTH

#include <errno.h>
#include <pwd.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <syslog.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/un.h>
#include <unistd.h>

#include <security/pam_ext.h>
#include <security/pam_modules.h>

#define REQUEST "{\"verb\": \"authenticate\", \"payload\": {}}\n"
#define OK_PREFIX "{\"ok\": true"
#define DEFAULT_TIMEOUT_SECONDS 20

static int socket_path(uid_t uid, char *buffer, size_t size) {
    int written = snprintf(buffer, size, "/run/user/%u/glance/auth.sock", (unsigned)uid);
    return written > 0 && (size_t)written < size;
}

static int read_line(int fd, char *buffer, size_t size) {
    size_t used = 0;
    while (used + 1 < size) {
        ssize_t got = read(fd, buffer + used, size - used - 1);
        if (got < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        if (got == 0) break;
        used += (size_t)got;
        if (memchr(buffer, '\n', used)) break;
    }
    buffer[used] = '\0';
    return (int)used;
}

PAM_EXTERN int pam_sm_authenticate(pam_handle_t *pamh, int flags, int argc, const char **argv) {
    (void)flags;
    int debug = 0;
    int timeout = DEFAULT_TIMEOUT_SECONDS;
    for (int i = 0; i < argc; i++) {
        if (strcmp(argv[i], "debug") == 0) debug = 1;
        else if (strncmp(argv[i], "timeout=", 8) == 0) timeout = atoi(argv[i] + 8);
    }

    const char *user = NULL;
    if (pam_get_user(pamh, &user, NULL) != PAM_SUCCESS || user == NULL || *user == '\0') {
        return PAM_AUTHINFO_UNAVAIL;
    }
    struct passwd *pw = getpwnam(user);
    if (pw == NULL) return PAM_AUTHINFO_UNAVAIL;

    if (getuid() != geteuid()) {
        if (debug) pam_syslog(pamh, LOG_NOTICE, "refusing: setuid context");
        return PAM_AUTHINFO_UNAVAIL;
    }
    if (getuid() != pw->pw_uid) {
        if (debug) pam_syslog(pamh, LOG_NOTICE, "refusing: caller uid %u is not %s", (unsigned)getuid(), user);
        return PAM_AUTHINFO_UNAVAIL;
    }

    char path[108];
    if (!socket_path(pw->pw_uid, path, sizeof path)) return PAM_AUTHINFO_UNAVAIL;

    struct stat st;
    if (stat(path, &st) != 0 || !S_ISSOCK(st.st_mode) || st.st_uid != pw->pw_uid) {
        if (debug) pam_syslog(pamh, LOG_NOTICE, "no daemon socket at %s", path);
        return PAM_AUTHINFO_UNAVAIL;
    }

    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd < 0) return PAM_AUTHINFO_UNAVAIL;
    struct timeval tv = { .tv_sec = timeout, .tv_usec = 0 };
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);

    struct sockaddr_un addr;
    memset(&addr, 0, sizeof addr);
    addr.sun_family = AF_UNIX;
    memcpy(addr.sun_path, path, strlen(path) + 1);  /* socket_path() already bounded it */
    if (connect(fd, (struct sockaddr *)&addr, sizeof addr) != 0) {
        if (debug) pam_syslog(pamh, LOG_NOTICE, "connect %s: %s", path, strerror(errno));
        close(fd);
        return PAM_AUTHINFO_UNAVAIL;
    }

    pam_info(pamh, "Glance: look at the camera");

    const char *request = REQUEST;
    size_t remaining = strlen(request);
    while (remaining > 0) {
        ssize_t sent = write(fd, request, remaining);
        if (sent < 0) {
            if (errno == EINTR) continue;
            close(fd);
            return PAM_AUTHINFO_UNAVAIL;
        }
        request += sent;
        remaining -= (size_t)sent;
    }

    char response[4096];
    int got = read_line(fd, response, sizeof response);
    close(fd);
    if (got <= 0) {
        if (debug) pam_syslog(pamh, LOG_NOTICE, "no response from daemon");
        return PAM_AUTHINFO_UNAVAIL;
    }

    if (strncmp(response, OK_PREFIX, strlen(OK_PREFIX)) == 0) {
        pam_syslog(pamh, LOG_INFO, "face unlock accepted for %s", user);
        return PAM_SUCCESS;
    }
    if (debug) pam_syslog(pamh, LOG_NOTICE, "daemon declined: %.200s", response);
    return PAM_AUTH_ERR;
}

PAM_EXTERN int pam_sm_setcred(pam_handle_t *pamh, int flags, int argc, const char **argv) {
    (void)pamh; (void)flags; (void)argc; (void)argv;
    return PAM_SUCCESS;
}
