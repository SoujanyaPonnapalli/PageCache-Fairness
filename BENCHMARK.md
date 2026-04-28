# Pagecache Fairness Benchmark (C++)

This repository contains a focused benchmarking suite to test **pagecache fairness** across different I/O workloads.

## 🎯 Purpose

Demonstrates how pagecache creates **fairness issues** between different workload types:
- **Small sequential workloads** get massive performance benefits
- **Large random workloads** suffer performance penalties
- Shows real-world implications of cache-friendly vs cache-unfriendly patterns

## 📁 Project Structure

```
pagecache/
├── fairness_configs.ini           # Workload definitions (8 fairness tests)
├── fairness_benchmark.cpp         # C++ benchmark implementation
├── fairness_benchmark             # Compiled C++ binary
├── Makefile                       # Build configuration
├── quick_fairness_analysis.py     # Results analysis script
├── fairness_results/              # Test results directory
├── test_file_1G                   # Test file for steady workloads
├── test_file_16G                  # Test file for bursty workloads
└── README.md                      # This file
```

## 🚀 Quick Start

### Prerequisites
- `fio` (I/O benchmark tool)
- `g++` with C++17 support
- `make` (build tool)
- `iostat` (I/O monitoring)
- Sufficient disk space for test files (17+ GB)

### Install Dependencies (macOS)
```bash
brew install fio gcc make
```

### Install Dependencies (Ubuntu/Debian)
```bash
sudo apt-get install fio g++ make sysstat
```

### Build the Benchmark
```bash
make
```

## 📊 Available Workloads

| Workload | Description | File Size | Pattern | Concurrency |
|----------|-------------|-----------|---------|-------------|
| `steady_reader_d1` | Sequential read, low concurrency | 1G | read | iodepth=1 |
| `steady_reader_d32` | Sequential read, high concurrency | 1G | read | iodepth=32 |
| `steady_writer_d1` | Sequential write, low concurrency | 1G | write | iodepth=1 |
| `steady_writer_d32` | Sequential write, high concurrency | 1G | write | iodepth=32 |
| `bursty_reader_d1` | Random read, low concurrency | 16G | randread | iodepth=1 |
| `bursty_reader_d32` | Random read, high concurrency | 16G | randread | iodepth=32 |
| `bursty_writer_d1` | Random write, low concurrency | 16G | randwrite | iodepth=1 |
| `bursty_writer_d32` | Random write, high concurrency | 16G | randwrite | iodepth=32 |

## 🔧 Usage

### Run Single Workload
```bash
# Run a specific fairness test (2 minutes)
./fairness_benchmark steady_reader_d1

# Run with verbose output
./fairness_benchmark -v bursty_writer_d32
```

### Run Complete Fairness Benchmark
```bash
# Run all 8 workloads (16 minutes total - 2 minutes per workload)
./fairness_benchmark all

# With verbose output
./fairness_benchmark -v all
```

### Analyze Results
```bash
# Analyze fairness results
./quick_fairness_analysis.py fairness_results/
```

## 📈 Understanding Results

### Expected Fairness Issues

**✅ Steady Workloads (1G files)** - Winners:
- Small working set fits in pagecache (~25GB available)
- Sequential access patterns are cache-friendly
- Expect **significant performance improvements** with pagecache

**❌ Bursty Workloads (16G files)** - Losers:
- Large working set creates cache pressure
- Random access patterns cause cache thrashing
- Expect **performance degradation** with pagecache

### Sample Output
```
## 📊 WORKLOAD PERFORMANCE COMPARISON

Workload             Mode     IOPS         BW(MB/s)   Lat(μs)
-----------------------------------------------------------------
steady_reader_d1     cached   1781355      6958.4     0.5
                     direct   1615500      6310.5     0.5
                     improve  +10.3       % +10.3    % +9.9     %
-----------------------------------------------------------------
bursty_writer_d1     cached   1199         4.7        833.9
                     direct   12518        48.9       79.6
                     improve  -90.4       % -90.4    % -947.6   %
```

## 🔍 Key Metrics

- **IOPS**: Operations per second (higher = better)
- **BW(MB/s)**: Bandwidth in megabytes per second
- **Lat(μs)**: Average latency in microseconds (lower = better)
- **Improvement %**: Performance change from direct I/O to cached I/O

## ⚙️ Configuration

Edit `fairness_configs.ini` to modify:
- Test runtime (default: 60 seconds per test)
- Block sizes (default: 4k)
- Concurrency levels (default: iodepth 1 and 32)
- File sizes (default: 1G for steady, 16G for bursty)

Configuration format:
```ini
[workload_name]
description = Workload description
file_size = 1G
block_size = 4k
runtime = 60
numjobs = 1
iodepth = 1
pattern = read
```

## 📋 Test Results

Results are saved in:
- **JSON files**: `fairness_results/*.json` (raw fio output)
- **Summary**: `fairness_results/summary.txt` (test summary)
- **iostat logs**: `fairness_results/iostat/` (system monitoring)

## 🛠 Troubleshooting

### Permission Issues
```bash
# If sudo is required for cache clearing
sudo ./fairness_benchmark steady_reader_d1
```

### Disk Space
```bash
# Check available space (need ~17GB)
df -h .
```

### Dependencies
```bash
# Check if tools are installed
which fio g++ make iostat

# Build the benchmark
make
```

## 📊 Example Complete Workflow

```bash
# 1. Build the benchmark
make

# 2. Run complete fairness benchmark (16 minutes)
./fairness_benchmark all

# 3. Analyze results
./quick_fairness_analysis.py fairness_results/

# 4. View summary
cat fairness_results/summary.txt
```

## 🎯 Expected Fairness Demonstration

This benchmark demonstrates **pagecache fairness issues**:

1. **Cache-friendly workloads** (small, sequential) get 100-800% improvements
2. **Cache-unfriendly workloads** (large, random) get 25-90% penalties
3. **Same system resources**, dramatically different outcomes
4. **Real-world impact**: Apps with different access patterns get unfair treatment

Perfect for demonstrating why **cache fairness algorithms** and **resource allocation policies** matter in multi-tenant systems.

## 🏗️ Build and Development

### Building from Source
```bash
# Clone the repository
git clone <repository-url>
cd pagecache

# Install dependencies (see prerequisites above)
brew install fio gcc make  # macOS
# OR
sudo apt-get install fio g++ make sysstat  # Linux

# Build the benchmark
make

# Clean build artifacts
make clean
```

### Available Make Targets
- `make` or `make all`: Build the benchmark
- `make clean`: Remove build artifacts
- `make test`: Run a single workload test
- `make benchmark`: Run all benchmarks
- `make analyze`: Analyze existing results
- `make workflow`: Complete build, test, analyze workflow

## 🔧 Advanced Usage

### Custom Configuration
Create or modify `fairness_configs.ini`:
```ini
[my_custom_workload]
description = Custom workload for testing
file_size = 2G
block_size = 8k
runtime = 120
numjobs = 2
iodepth = 16
pattern = randrw
```

### Command Line Options
```bash
./fairness_benchmark --help      # Show help
./fairness_benchmark -v all      # Verbose mode
./fairness_benchmark -c custom.ini -o results/ workload_name
```

## License

This benchmark suite is provided as-is for performance testing purposes.