#include <errno.h>
#include <inttypes.h>
#include <signal.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

typedef struct {
    /* kept for compatibility if needed */
    int dummy;
} thread_ctx_t;

static volatile sig_atomic_t keep_running = 1;

static void handle_signal(int sig) {
    (void)sig;
    keep_running = 0;
}

static double now_seconds(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + ((double)ts.tv_nsec / 1e9);
}

static void print_usage(const char *prog) {
    fprintf(stderr,
        "Usage: %s [--threads N] [--duration S] [--size N]\n"
        "  --threads N   Number of threads (default: online CPUs)\n"
        "  --duration S  Duration in seconds (default: 30)\n"
        "  --size N      Number of elements per array (default: 50000000)\n",
        prog
    );
}

/* worker_stream_triad removed: single-threaded execution enforced */

int main(int argc, char *argv[]) {
    int num_threads = 1; // force single-thread
    int duration = 30;
    size_t array_size = 50000000UL; // 50M doubles ~ 400 MB per array

    for (int i = 1; i < argc; ++i) {
        if (strcmp(argv[i], "--threads") == 0 && i + 1 < argc) {
            // threads argument ignored: single-thread enforced
            i++; // skip value
        } else if (strcmp(argv[i], "--duration") == 0 && i + 1 < argc) {
            duration = atoi(argv[++i]);
        } else if (strcmp(argv[i], "--size") == 0 && i + 1 < argc) {
            array_size = (size_t)strtoull(argv[++i], NULL, 10);
        } else if (strcmp(argv[i], "--help") == 0 || strcmp(argv[i], "-h") == 0) {
            print_usage(argv[0]);
            return 0;
        } else {
            fprintf(stderr, "Unknown argument: %s\n", argv[i]);
            print_usage(argv[0]);
            return 1;
        }
    }

    if (num_threads <= 0 || duration <= 0 || array_size == 0) {
        fprintf(stderr, "Invalid arguments\n");
        return 1;
    }

    signal(SIGINT, handle_signal);
    signal(SIGTERM, handle_signal);

    double *a = NULL;
    double *b = NULL;
    double *c = NULL;

    if (posix_memalign((void **)&a, 64, array_size * sizeof(double)) != 0 ||
        posix_memalign((void **)&b, 64, array_size * sizeof(double)) != 0 ||
        posix_memalign((void **)&c, 64, array_size * sizeof(double)) != 0) {
        fprintf(stderr, "Error allocating arrays\n");
        free(a);
        free(b);
        free(c);
        return 1;
    }

    for (size_t i = 0; i < array_size; ++i) {
        a[i] = 0.0;
        b[i] = 1.0;
        c[i] = 2.0;
    }

    const double scalar = 3.0;
    double t_start = now_seconds();

    // Single-threaded execution: run the triad kernel repeatedly in this process
    uint64_t total_iterations = 0;
    while (keep_running) {
        double now = now_seconds();
        if (now - t_start >= (double)duration) break;

        for (size_t i = 0; i < array_size; ++i) {
            a[i] = b[i] + scalar * c[i];
        }
        total_iterations++;
    }

    double t_end = now_seconds();
    double elapsed = t_end - t_start;

    // STREAM Triad: 2 reads + 1 write per element = 24 bytes/element for double
    uint64_t total_elements = total_iterations * (uint64_t)array_size;
    uint64_t total_bytes = total_elements * 3ULL * (uint64_t)sizeof(double);
    double bytes_per_sec = (elapsed > 0.0) ? ((double)total_bytes / elapsed) : 0.0;
    double bandwidth_gbps = bytes_per_sec / 1e9;

    printf(
        "{"
        "\"benchmark\":\"memory-stream-hpc\","
        "\"kernel\":\"stream_triad\","
        "\"threads\":%d,"
        "\"duration_target_s\":%d,"
        "\"duration_real_s\":%.6f,"
        "\"array_size\":%zu,"
        "\"iterations\":%" PRIu64 ","
        "\"elements_processed\":%" PRIu64 ","
        "\"bytes_processed\":%" PRIu64 ","
        "\"bytes_per_sec\":%.6f,"
        "\"bandwidth_gbps\":%.6f"
        "}\n",
        1,
        duration,
        elapsed,
        array_size,
        total_iterations,
        total_elements,
        total_bytes,
        bytes_per_sec,
        bandwidth_gbps
    );

    /* no thread structures to free in single-thread mode */
    free(a);
    free(b);
    free(c);
    return 0;
}
