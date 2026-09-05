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

typedef struct { int dummy; } thread_ctx_t;

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
        "  --size N      Matrix size N for NxN matrices (default: 1024)\n",
        prog
    );
}

static void init_matrix(double *m, int n, double seed) {
    const size_t total = (size_t)n * (size_t)n;
    for (size_t i = 0; i < total; ++i) {
        m[i] = seed + (double)(i % 100) * 0.001;
    }
}

/* worker_dgemm removed: single-threaded execution enforced */

int main(int argc, char *argv[]) {
    int num_threads = 1; // force single-thread
    int duration = 30;
    int n = 1024;

    for (int i = 1; i < argc; ++i) {
        if (strcmp(argv[i], "--threads") == 0 && i + 1 < argc) {
            // threads argument ignored: single-thread enforced
            i++; // skip value
        } else if (strcmp(argv[i], "--duration") == 0 && i + 1 < argc) {
            duration = atoi(argv[++i]);
        } else if (strcmp(argv[i], "--size") == 0 && i + 1 < argc) {
            n = atoi(argv[++i]);
        } else if (strcmp(argv[i], "--help") == 0 || strcmp(argv[i], "-h") == 0) {
            print_usage(argv[0]);
            return 0;
        } else {
            fprintf(stderr, "Unknown argument: %s\n", argv[i]);
            print_usage(argv[0]);
            return 1;
        }
    }

    if (num_threads <= 0 || duration <= 0 || n <= 0) {
        fprintf(stderr, "Invalid arguments\n");
        return 1;
    }

    signal(SIGINT, handle_signal);
    signal(SIGTERM, handle_signal);

    const size_t total_elems = (size_t)n * (size_t)n;
    const size_t bytes = total_elems * sizeof(double);

    double *a = NULL;
    double *b = NULL;
    double *c = NULL;

    if (posix_memalign((void **)&a, 64, bytes) != 0 ||
        posix_memalign((void **)&b, 64, bytes) != 0 ||
        posix_memalign((void **)&c, 64, bytes) != 0) {
        fprintf(stderr, "Error allocating matrices\n");
        free(a);
        free(b);
        free(c);
        return 1;
    }

    init_matrix(a, n, 1.0);
    init_matrix(b, n, 2.0);
    init_matrix(c, n, 0.0);

    double t_start = now_seconds();

    // Single-threaded DGEMM-like workload: compute full matrix multiply repeatedly
    uint64_t total_iterations = 0;
    while (keep_running) {
        double now = now_seconds();
        if (now - t_start >= (double)duration) break;

        for (int i = 0; i < n; ++i) {
            for (int j = 0; j < n; ++j) {
                double sum = c[(size_t)i * (size_t)n + (size_t)j];
                for (int k = 0; k < n; ++k) {
                    sum += a[(size_t)i * (size_t)n + (size_t)k] *
                           b[(size_t)k * (size_t)n + (size_t)j];
                }
                c[(size_t)i * (size_t)n + (size_t)j] = sum;
            }
        }
        total_iterations++;
    }

    double t_end = now_seconds();
    double elapsed = t_end - t_start;

    // DGEMM: 2 * N^3 floating-point ops per full multiplication
    double flops_per_iteration = 2.0 * (double)n * (double)n * (double)n;
    double total_flops = flops_per_iteration * (double)total_iterations;
    double gflops = (elapsed > 0.0) ? (total_flops / elapsed / 1e9) : 0.0;

    double checksum = 0.0;
    for (int i = 0; i < n; i += (n / 16 > 0 ? n / 16 : 1)) {
        for (int j = 0; j < n; j += (n / 16 > 0 ? n / 16 : 1)) {
            checksum += c[(size_t)i * (size_t)n + (size_t)j];
        }
    }

    double ops_per_sec = (elapsed > 0.0) ? (total_flops / elapsed) : 0.0;

    printf(
        "{"
        "\"benchmark\":\"compute-dgemm-hpc\","
        "\"kernel\":\"dgemm_like\","
        "\"threads\":%d,"
        "\"duration_target_s\":%d,"
        "\"duration_real_s\":%.6f,"
        "\"matrix_size\":%d,"
        "\"iterations\":%" PRIu64 ","
        "\"ops\":%.0f,"
        "\"ops_per_sec\":%.6f,"
        "\"gflops\":%.6f,"
        "\"checksum\":%.6f"
        "}\n",
        1,
        duration,
        elapsed,
        n,
        total_iterations,
        total_flops,
        ops_per_sec,
        gflops,
        checksum
    );

    /* no thread structures to free in single-thread mode */
    free(a);
    free(b);
    free(c);
    return 0;
}
