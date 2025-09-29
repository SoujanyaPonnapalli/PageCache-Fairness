# Makefile for Fairness Benchmark C++ Implementation

CXX = g++
CXXFLAGS = -std=c++17 -Wall -Wextra -O2
TARGET = fairness_benchmark
SOURCE = fairness_benchmark.cpp

# Default target
all: $(TARGET)

$(TARGET): $(SOURCE)
	$(CXX) $(CXXFLAGS) -o $(TARGET) $(SOURCE)

# Clean built files
clean:
	rm -f $(TARGET)

# Install system dependencies
install-deps:
	@echo "Installing dependencies..."
	@if command -v brew >/dev/null 2>&1; then \
		echo "Installing fio via brew..."; \
		brew install fio; \
	elif command -v apt-get >/dev/null 2>&1; then \
		echo "Installing fio via apt..."; \
		sudo apt-get update && sudo apt-get install -y fio; \
	elif command -v dnf >/dev/null 2>&1; then \
		echo "Installing fio via dnf..."; \
		sudo dnf install -y fio; \
	else \
		echo "Please install fio manually for your system"; \
	fi

# Test the benchmark (single workload)
test: $(TARGET)
	./$(TARGET) steady_reader_d1

# Run all benchmarks
benchmark: $(TARGET)
	./$(TARGET) all

# Analyze results
analyze:
	./quick_fairness_analysis.py fairness_results/

# Complete workflow: build, run, analyze
workflow: $(TARGET)
	@echo "=== Building C++ Fairness Benchmark ==="
	@echo "✓ Binary compiled successfully"
	@echo ""
	@echo "=== Running Single Test ==="
	./$(TARGET) -v steady_reader_d1
	@echo ""
	@echo "=== Analyzing Results ==="
	./quick_fairness_analysis.py fairness_results/

.PHONY: all clean install-deps test benchmark analyze workflow