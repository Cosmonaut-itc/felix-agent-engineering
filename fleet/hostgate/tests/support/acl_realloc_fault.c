/* R1-C1: fail only realloc inside the real Darwin fstatx_np call. */
#include <sys/stat.h>
#include <fcntl.h>
#include <stdlib.h>
#include <errno.h>

static int armed, failures;
static void *deny_realloc(void *ptr, size_t size) {
    if (armed) { failures++; errno = ENOMEM; return NULL; }
    return realloc(ptr, size);
}
__attribute__((used)) static struct { const void *replacement, *original; } interpose
    __attribute__((section("__DATA,__interpose"))) = {
        (const void *)deny_realloc, (const void *)realloc
    };
int failing_fstatx(int fd, void *buf, void *security) {
    armed = 1;
    int rc = fstatx_np(fd, buf, security);
    armed = 0;
    return rc;
}
int get_failures(void) { return failures; }
