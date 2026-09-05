CC      := gcc
CFLAGS  := -O3 -Wall -Wextra -march=native
CLIBS   := -lm -pthread

RM      := rm -f
MKDIR_P := mkdir -p

C_DIR := benchmarks/c

C_BENCHES  := compute-dgemm-hpc memory-stream-hpc

C_TARGETS  := $(foreach p,$(C_BENCHES),$(C_DIR)/$(p)/$(p))

.PHONY: all c dirs clean help generate-subdir-makefiles

all: dirs c

c: $(C_TARGETS)

dirs:
	$(MKDIR_P) results results/logs results/raw slurm/generated

clean:
	$(RM) $(C_TARGETS) $(GO_TARGETS)

help:
	@echo "Targets disponibles:"
	@echo "  make / make all  - compila benchmarks C y Go"
	@echo "  make c           - compila solo benchmarks C"
	@echo "  make dirs        - crea directorios necesarios"
	@echo "  make clean       - elimina binarios generados"

define C_template
$(C_DIR)/$(1)/$(1): $(C_DIR)/$(1)/$(1).c
	$(CC) $(CFLAGS) $$< -o $$@ $(CLIBS)
endef

$(foreach b,$(C_BENCHES),$(eval $(call C_template,$(b))))
