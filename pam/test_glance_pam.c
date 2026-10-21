/*
 * Tests for the module's decision helpers.
 *
 * The module is a PAM plugin, so its helpers are static; this includes the
 * source and calls them directly. Nothing here talks to the daemon or to
 * PAM: what is being tested is the part that decides whether a setuid
 * conversation is allowed to reach the daemon at all.
 *
 *   make test_glance_pam && ./test_glance_pam
 */

#include "pam_glance.c"

#include <stdio.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

static int failures = 0;

static void check(int condition, const char *what) {
    if (condition) {
        printf("  ok: %s\n", what);
    } else {
        printf("  FAIL: %s\n", what);
        failures += 1;
    }
}

int main(int argc, char **argv) {
    (void)argc;
    /* The child runs with a command line of its own, so wants_interactive()
       can be asked about one that really has a -n in it. */
    if (getenv("GLANCE_TEST_CHILD")) {
        return wants_interactive() ? 1 : 0;
    }

    printf("service matching\n");
    check(service_matches("sudo", "sudo"), "sudo matches sudo");
    check(service_matches("sudo-i", "sudo"), "sudo-i matches sudo");
    check(!service_matches("su", "sudo"), "su does not match sudo");
    check(!service_matches("sudoedit", "sudo"), "sudoedit does not match sudo");
    check(!service_matches("xsudo", "sudo"), "xsudo does not match sudo");
    check(!service_matches(NULL, "sudo"), "no service matches nothing");

    printf("the program a service belongs to\n");
    char program[SERVICE_NAME_MAX + 1];
    service_program("sudo", program, sizeof(program));
    check(strcmp(program, "sudo") == 0, "the program of sudo is sudo");
    service_program("sudo-i", program, sizeof(program));
    check(strcmp(program, "sudo") == 0, "the program of sudo-i is sudo");
    service_program("su", program, sizeof(program));
    check(strcmp(program, "su") == 0, "the program of su is su");
    service_program(NULL, program, sizeof(program));
    check(program[0] == '\0', "no service has no program");

    printf("the request\n");
    char request[256];
    build_request(request, sizeof(request), "sudo");
    check(strstr(request, "\"verb\": \"authenticate\"") != NULL, "the verb is the one the daemon answers");
    check(strstr(request, "\"service\": \"sudo\"") != NULL, "the service rides along");

    build_request(request, sizeof(request), "su\"it");
    check(strstr(request, "\"service\": \"suit\"") != NULL, "a quote in the service name is dropped, not carried");
    int quotes = 0;
    for (const char *c = request; *c; c++) quotes += (*c == '"');
    check(quotes % 2 == 0, "the request is still one JSON document");

    printf("the caller's own command line\n");
    check(wants_interactive(), "this process is asking to be prompted");

    setenv("GLANCE_TEST_CHILD", "1", 1);

    pid_t child = fork();
    if (child == 0) {
        execl(argv[0], argv[0], "-n", (char *)NULL);
        _exit(127);
    }
    int status = 0;
    waitpid(child, &status, 0);
    check(WIFEXITED(status) && WEXITSTATUS(status) == 0, "-n is recognised and refused");

    child = fork();
    if (child == 0) {
        /* A command with a flag of its own: sudo rm -n is not sudo -n. */
        execl(argv[0], argv[0], "rm", "-n", (char *)NULL);
        _exit(127);
    }
    waitpid(child, &status, 0);
    check(WIFEXITED(status) && WEXITSTATUS(status) == 1, "a flag after the command is not sudo's own");

    printf("the terminal\n");
    printf("  (this process %s a terminal)\n", has_terminal() ? "has" : "does not have");

    printf("\n%s\n", failures == 0 ? "all checks passed" : "there were failures");
    return failures == 0 ? 0 : 1;
}
