CC      ?= gcc
CFLAGS  ?= -std=c11 -O2 -Wall -Wextra -D_GNU_SOURCE
LDFLAGS ?= -lm
TARGET   = benchmark
SRC      = benchmark.c

.PHONY: all clean test run-all analyze workflow setup-files

all: $(TARGET)

$(TARGET): $(SRC)
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

clean:
	rm -f $(TARGET)

# Create dense test_file_1G / test_file_8G once (reuse across experiments)
setup-files:
	bash ./setup_test_files.sh

# Run a single-workload baseline
test: $(TARGET)
	./$(TARGET) -v client1_steady

# Run every workload defined in the config
run-all: $(TARGET)
	./$(TARGET) all

# Analyze existing results
analyze:
	./benchmark_analysis.py benchmark_results/

# Full build -> ensure files -> dual experiment -> analyze
workflow: $(TARGET) setup-files
	./$(TARGET) -m cached dual
	./benchmark_analysis.py benchmark_results/
