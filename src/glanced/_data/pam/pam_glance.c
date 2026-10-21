/*
 * pam_glance — ask the user's glanced daemon for a face-unlock verdict.
 *
 * The module does no vision work. It connects to the daemon's auth socket in
 * the user's runtime directory, sends one `authenticate` request, and maps
 * the answer onto PAM_SUCCESS / PAM_AUTH_ERR. Everything that decides is in
 * the daemon (see src/glanced/daemon.py); this file is deliberately dumb.
 *
 * Two kinds of caller use it:
 *
 *   - The session lock screen, which runs as you. The module refuses unless
 *     the process's real uid IS the account being authenticated.
 *   - `sudo`, which runs setuid root. That is a different trust question
 *     entirely, so it is off by default and has to be asked for, per stack,
 *     with `allow_setuid`.
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
 * The setuid rail is the one `allow_setuid` lifts, and it is deliberately
 * narrow. Every one of these has to hold, or the module declines and the
 * stack carries on to the password:
 *
 *   - the PAM service is one the stack asked for by name (default: `sudo`);
 *   - the program talking to us is that service's own program: the
 *     conversation runs inside the `sudo` process, and /proc/self/comm says
 *     so. A PAM service file that named `sudo` but was loaded by anything
 *     else is refused;
 *   - there is a terminal on stdin, so a face is being looked at by someone
 *     at a keyboard rather than asked for by a script;
 *   - sudo was not asked not to prompt (`-n`, `--non-interactive`), because
 *     that flag means "never talk to me", and a scan is a conversation.
 *
 * What this does not fix: the daemon runs as you, and it will say yes to a
 * face it recognises. So any program that can run `sudo` can, in principle,
 * wait for you to walk past the camera. That is the same bargain Howdy
 * makes, and it is why the module only ever turns a verdict into an answer
 * inside a `sudo` conversation that a human started at a terminal.
 */

#define PAM_SM_AUTH

#include <errno.h>
#include <fcntl.h>
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

#define OK_PREFIX "{\"ok\": true"
#define DEFAULT_TIMEOUT_SECONDS 20

/*: How many PAM service names one stack may claim. */
#define MAX_ALLOWED_SERVICES 8

/*: Longest service name copied into the request payload. */
#define SERVICE_NAME_MAX 32

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

/* The PAM service that is asking, e.g. "sudo". */
static const char *pam_service_name(pam_handle_t *pamh) {
    const void *item = NULL;
    if (pam_get_item(pamh, PAM_SERVICE, &item) != PAM_SUCCESS) return NULL;
    return (const char *)item;
}

/*
 * "sudo" claims the service "sudo"; "sudo -i" asks for the service
 * "sudo-i". A service matches a claim when it is the claim, or the claim
 * followed by a dash and something else.
 */
static int service_matches(const char *service, const char *claim) {
    if (service == NULL || claim == NULL) return 0;
    size_t length = strlen(claim);
    if (strncmp(service, claim, length) != 0) return 0;
    return service[length] == '\0' || service[length] == '-';
}

/* The program a service belongs to: "sudo-i" belongs to "sudo". */
static void service_program(const char *service, char *buffer, size_t size) {
    size_t used = 0;
    if (service == NULL) {
        buffer[0] = '\0';
        return;
    }
    while (service[used] != '\0' && service[used] != '-' && used + 1 < size) {
        buffer[used] = service[used];
        used++;
    }
    buffer[used] = '\0';
}

/* The name of the program running this conversation, from /proc/self/comm. */
static int process_comm(char *buffer, size_t size) {
    int fd = open("/proc/self/comm", O_RDONLY | O_CLOEXEC);
    if (fd < 0) return -1;
    ssize_t got = read(fd, buffer, size - 1);
    close(fd);
    if (got <= 0) return -1;
    buffer[got] = '\0';
    while (got > 0 && (buffer[got - 1] == '\n' || buffer[got - 1] == '\r')) {
        buffer[--got] = '\0';
    }
    return 0;
}

/*
 * Whether sudo was told not to prompt (`-n`, `--non-interactive`). That flag
 * means "never ask me anything", and a face scan is a question, so it is
 * not one to ask under it.
 *
 * Only the options before the command are read: `sudo rm -n` is a command
 * with a flag of its own, not a sudo that was told not to prompt.
 */
static int wants_interactive(void) {
    int fd = open("/proc/self/cmdline", O_RDONLY | O_CLOEXEC);
    if (fd < 0) return 1; /* unknown: do not block the caller on a guess */
    char buffer[4096];
    ssize_t got = read(fd, buffer, sizeof(buffer) - 1);
    close(fd);
    if (got <= 0) return 1;
    buffer[got] = '\0';

    ssize_t offset = 0;
    /* Skip argv[0]: the program itself is not one of its options. */
    if (offset < got) offset += (ssize_t)strlen(buffer + offset) + 1;

    while (offset < got) {
        const char *argument = buffer + offset;
        if (argument[0] != '-') break;         /* the command: the rest is its own */
        if (strcmp(argument, "-n") == 0 || strcmp(argument, "--non-interactive") == 0) return 0;
        offset += (ssize_t)strlen(argument) + 1;
    }
    return 1;
}

/*
 * Whether there is a terminal to be at. The controlling terminal is what
 * matters: sudo reads its password from /dev/tty rather than from stdin, so
 * a terminal session is a terminal even when stdin is a file. /proc/self/tty
 * names it, and says "none" when there is not one — which is the case for
 * cron, for a systemd service, and for anything else that is not a person
 * at a keyboard.
 */
static int has_terminal(void) {
    char buffer[64];
    int fd = open("/proc/self/tty", O_RDONLY | O_CLOEXEC);
    if (fd >= 0) {
        ssize_t got = read(fd, buffer, sizeof(buffer) - 1);
        close(fd);
        if (got > 0) {
            buffer[got] = '\0';
            return strncmp(buffer, "none", 4) != 0;
        }
    }
    return isatty(STDIN_FILENO);
}

/*
 * The request. The service name rides along so the daemon — and its log —
 * can tell a lock screen from a privilege request. It comes from a
 * root-owned PAM file, but it lands in JSON, so it is filtered down to
 * characters that cannot end the string.
 */
static void build_request(char *buffer, size_t size, const char *service) {
    char safe[SERVICE_NAME_MAX + 1];
    size_t used = 0;
    for (size_t i = 0; service != NULL && service[i] != '\0' && used < SERVICE_NAME_MAX; i++) {
        char c = service[i];
        if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') ||
            c == '.' || c == '_' || c == '-') {
            safe[used++] = c;
        }
    }
    safe[used] = '\0';
    snprintf(buffer, size, "{\"verb\": \"authenticate\", \"payload\": {\"service\": \"%s\"}}\n", safe);
}

/*
 * Whether this setuid conversation is one the stack asked for. Everything
 * here is a refusal, so the answer is "no" unless every check passes.
 */
static int setuid_allowed(pam_handle_t *pamh, const char *service,
                           const char *const *allowed, size_t allowed_count,
                           int debug) {
    if (service == NULL) {
        if (debug) pam_syslog(pamh, LOG_NOTICE, "refusing: no PAM service name");
        return 0;
    }

    /* A setuid context that is not root is not the one this is for. */
    if (geteuid() != 0) {
        if (debug) pam_syslog(pamh, LOG_NOTICE, "refusing: setuid context is not root");
        return 0;
    }

    int permitted = 0;
    for (size_t i = 0; i < allowed_count; i++) {
        if (service_matches(service, allowed[i])) {
            permitted = 1;
            break;
        }
    }
    if (!permitted) {
        if (debug) pam_syslog(pamh, LOG_NOTICE, "refusing: service %s is not allowed here", service);
        return 0;
    }

    char program[SERVICE_NAME_MAX + 1];
    service_program(service, program, sizeof(program));

    char comm[SERVICE_NAME_MAX + 1];
    if (process_comm(comm, sizeof(comm)) != 0) {
        if (debug) pam_syslog(pamh, LOG_NOTICE, "refusing: cannot read /proc/self/comm");
        return 0;
    }
    if (strcmp(comm, program) != 0) {
        if (debug) pam_syslog(pamh, LOG_NOTICE, "refusing: caller is %s, not %s", comm, program);
        return 0;
    }

    if (!has_terminal()) {
        if (debug) pam_syslog(pamh, LOG_NOTICE, "refusing: no terminal");
        return 0;
    }

    if (!wants_interactive()) {
        if (debug) pam_syslog(pamh, LOG_NOTICE, "refusing: the caller asked not to be prompted");
        return 0;
    }

    return 1;
}

PAM_EXTERN int pam_sm_authenticate(pam_handle_t *pamh, int flags, int argc, const char **argv) {
    (void)flags;
    int debug = 0;
    int timeout = DEFAULT_TIMEOUT_SECONDS;
    int allow_setuid = 0;
    const char *allowed[MAX_ALLOWED_SERVICES];
    size_t allowed_count = 0;

    for (int i = 0; i < argc; i++) {
        if (strcmp(argv[i], "debug") == 0) debug = 1;
        else if (strncmp(argv[i], "timeout=", 8) == 0) timeout = atoi(argv[i] + 8);
        else if (strcmp(argv[i], "allow_setuid") == 0) allow_setuid = 1;
        else if (strncmp(argv[i], "service=", 8) == 0 && allowed_count < MAX_ALLOWED_SERVICES) {
            allowed[allowed_count++] = argv[i] + 8;
        }
    }
    if (allowed_count == 0) {
        /* The only setuid caller this module is meant for. */
        allowed[allowed_count++] = "sudo";
    }

    const char *user = NULL;
    if (pam_get_user(pamh, &user, NULL) != PAM_SUCCESS || user == NULL || *user == '\0') {
        return PAM_AUTHINFO_UNAVAIL;
    }
    struct passwd *pw = getpwnam(user);
    if (pw == NULL) return PAM_AUTHINFO_UNAVAIL;

    const char *service = pam_service_name(pamh);
    int privileged = 0;

    if (getuid() != geteuid()) {
        /*
         * A setuid context: `sudo`, `su`, a login manager. The rails above
         * are why this is refused by default. `allow_setuid` lifts them for
         * one named service, and only for that service's own program.
         */
        if (!allow_setuid) {
            if (debug) pam_syslog(pamh, LOG_NOTICE, "refusing: setuid context");
            return PAM_AUTHINFO_UNAVAIL;
        }
        if (!setuid_allowed(pamh, service, allowed, allowed_count, debug)) {
            return PAM_AUTHINFO_UNAVAIL;
        }
        privileged = 1;
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

    /* The lock screen's indicator watches for this prefix, and a terminal
       needs to be told what is wanted of it. */
    pam_info(pamh, "Glance: look at the camera");

    char request[256];
    build_request(request, sizeof(request), service);

    size_t remaining = strlen(request);
    const char *cursor = request;
    while (remaining > 0) {
        ssize_t sent = write(fd, cursor, remaining);
        if (sent < 0) {
            if (errno == EINTR) continue;
            close(fd);
            return PAM_AUTHINFO_UNAVAIL;
        }
        cursor += sent;
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
        if (privileged) {
            /* The command is about to run as root; say who let it. */
            pam_info(pamh, "Glance: face verified");
        }
        return PAM_SUCCESS;
    }
    if (debug) pam_syslog(pamh, LOG_NOTICE, "daemon declined: %.200s", response);
    return PAM_AUTH_ERR;
}

PAM_EXTERN int pam_sm_setcred(pam_handle_t *pamh, int flags, int argc, const char **argv) {
    (void)pamh; (void)flags; (void)argc; (void)argv;
    return PAM_SUCCESS;
}
