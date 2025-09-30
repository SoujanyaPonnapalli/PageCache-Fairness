#include <iostream>
#include <string>
#include <vector>
#include <map>
#include <fstream>
#include <filesystem>
#include <chrono>
#include <iomanip>
#include <sstream>
#include <cstdlib>
#include <unistd.h>
#include <sys/wait.h>
#include <signal.h>
#include <cstring>

namespace fs = std::filesystem;

struct PhaseConfig {
    int runtime;
    std::string block_size;
    int iodepth;
    std::string pattern;
    std::string ioengine;
    int rate_iops;        // Per-phase rate_iops (0 = unlimited)
};

struct WorkloadConfig {
    std::string description;
    std::string file_size;
    int numjobs;
    int rate_iops;        // Workload-level rate_iops (0 = unlimited)
    // Legacy single-phase config (for backward compatibility)
    std::string block_size;
    int runtime;
    int iodepth;
    std::string pattern;
    std::string ioengine;
    // Multi-phase config
    std::vector<PhaseConfig> phases;
};

class FairnessBenchmark {
private:
    std::string config_file;
    std::string output_dir;
    bool verbose;
    std::map<std::string, WorkloadConfig> workloads;

    std::string get_timestamp() {
        auto now = std::chrono::system_clock::now();
        auto time_t = std::chrono::system_clock::to_time_t(now);
        std::stringstream ss;
        ss << std::put_time(std::localtime(&time_t), "%Y%m%d_%H%M%S");
        return ss.str();
    }

    void log(const std::string& message) {
        auto now = std::chrono::system_clock::now();
        auto time_t = std::chrono::system_clock::to_time_t(now);
        std::cout << "[" << std::put_time(std::localtime(&time_t), "%H:%M:%S")
                  << "] " << message << std::endl;
    }

    // Helper to suppress unused result warnings for system calls we don't care about
    void run_system(const std::string& cmd) {
        [[maybe_unused]] int result = system(cmd.c_str());
    }

    bool check_dependencies() {
        if (system("which fio > /dev/null 2>&1") != 0) {
            log("ERROR: fio is required but not installed");
            return false;
        }

        if (!fs::exists(config_file)) {
            log("ERROR: Config file not found: " + config_file);
            return false;
        }

        return true;
    }

    void setup() {
        log("Setting up fairness benchmark...");

        if (fs::exists(output_dir)) {
            fs::remove_all(output_dir);
        }
        fs::create_directories(output_dir);
        fs::create_directories(output_dir + "/iostat");

        // Create metadata
        std::ofstream metadata(output_dir + "/metadata.txt");
        metadata << "timestamp=" << get_timestamp() << std::endl;
        metadata << "config_file=" << config_file << std::endl;
        metadata << "test_type=fairness_benchmark" << std::endl;
        metadata.close();
    }

    void drop_caches() {
        run_system("sync");
        run_system("sudo purge 2>/dev/null || true");
        sleep(1);
    }

    uintmax_t get_size_bytes(const std::string& size_str) {
        if (size_str == "1G") return 1ULL * 1024 * 1024 * 1024;
        if (size_str == "16G") return 16ULL * 1024 * 1024 * 1024;
        return 0;
    }

    void create_test_file(const std::string& file_size, const std::string& test_file) {
        if (fs::exists(test_file)) {
            auto actual_size = fs::file_size(test_file);
            auto expected_size = get_size_bytes(file_size);

            if (actual_size >= expected_size) {
                log("Using existing " + file_size + " test file: " + test_file);
                return;
            }
        }

        log("Creating " + file_size + " test file: " + test_file);
        std::string cmd;
        if (file_size == "1G") {
            cmd = "dd if=/dev/zero of=" + test_file + " bs=1M count=1024 2>/dev/null";
        } else if (file_size == "16G") {
            cmd = "dd if=/dev/zero of=" + test_file + " bs=1M count=16384 2>/dev/null";
        } else {
            log("ERROR: Unsupported file size: " + file_size);
            exit(1);
        }
        run_system(cmd);
        log("Test file created: " + test_file);
    }

    bool run_workload(const std::string& workload_name) {
        auto it = workloads.find(workload_name);
        if (it == workloads.end()) {
            log("ERROR: Workload '" + workload_name + "' not found in config");
            return false;
        }

        const auto& config = it->second;
        log("Running workload: " + workload_name);

        // Determine if this is a multi-phase workload
        bool is_multi_phase = !config.phases.empty();

        if (is_multi_phase) {
            log("  Multi-phase workload with " + std::to_string(config.phases.size()) + " phases");
        } else if (verbose) {
            log("  Config: " + config.file_size + ", " + config.block_size +
                ", jobs=" + std::to_string(config.numjobs) +
                ", depth=" + std::to_string(config.iodepth) +
                ", pattern=" + config.pattern);
        }

        // Create test file
        std::string script_dir = fs::current_path().string();
        std::string test_file = script_dir + "/test_file_" + config.file_size;
        create_test_file(config.file_size, test_file);

        // Test both cached and direct modes
        std::vector<std::string> cache_modes = {"cached", "direct"};
        for (const auto& cache_mode : cache_modes) {
            std::string test_name = workload_name + "_" + cache_mode;
            std::string output_file = output_dir + "/" + test_name + ".json";
            std::string iostat_file = output_dir + "/iostat/" + test_name + ".iostat";

            log("  Running: " + test_name);

            // Start iostat monitoring
            pid_t iostat_pid = fork();
            if (iostat_pid == 0) {
                [[maybe_unused]] FILE* out = freopen(iostat_file.c_str(), "w", stdout);
                [[maybe_unused]] FILE* err = freopen("/dev/null", "w", stderr);
                execl("/usr/bin/iostat", "iostat", "-d", "-w", "1", nullptr);
                exit(1);
            }

            drop_caches();

            if (is_multi_phase) {
                // Run phases sequentially
                for (size_t phase_idx = 0; phase_idx < config.phases.size(); phase_idx++) {
                    const auto& phase = config.phases[phase_idx];
                    std::string phase_name = test_name + "_phase" + std::to_string(phase_idx + 1);
                    std::string phase_output = output_dir + "/" + phase_name + ".json";

                    // Use per-phase values with fallback to workload defaults
                    int phase_rate_iops = (phase.rate_iops > 0) ? phase.rate_iops : config.rate_iops;

                    std::string phase_info = "    Phase " + std::to_string(phase_idx + 1) + "/" + std::to_string(config.phases.size()) +
                        ": " + phase.pattern + " for " + std::to_string(phase.runtime) + "s";
                    if (phase_rate_iops > 0) {
                        phase_info += " (rate_iops=" + std::to_string(phase_rate_iops) + ")";
                    }
                    log(phase_info);

                    // Build fio command for this phase
                    std::ostringstream fio_cmd;
                    fio_cmd << "fio"
                            << " --name=" << phase_name
                            << " --filename=" << test_file
                            << " --size=" << config.file_size
                            << " --runtime=" << phase.runtime
                            << " --time_based=1"
                            << " --rw=" << phase.pattern
                            << " --bs=" << phase.block_size
                            << " --numjobs=" << config.numjobs
                            << " --iodepth=" << phase.iodepth;

                    if (!phase.ioengine.empty()) {
                        fio_cmd << " --ioengine=" << phase.ioengine;
                    }

                    if (phase_rate_iops > 0) {
                        fio_cmd << " --rate_iops=" << phase_rate_iops;
                    }

                    fio_cmd << " --group_reporting=1"
                            << " --output-format=json"
                            << " --output=" << phase_output
                            << " --status-interval=5";

                    if (cache_mode == "direct") {
                        fio_cmd << " --direct=1";
                    }

                    // Run phase
                    if (verbose) {
                        log("    Executing: " + fio_cmd.str());
                        run_system(fio_cmd.str());
                    } else {
                        std::string silent_cmd = fio_cmd.str() + " >/dev/null 2>&1";
                        run_system(silent_cmd);
                    }

                    // Don't drop caches between phases - maintain state
                }

                // Merge phase results into single output file (simplified: use last completed phase)
                // In production, you'd want to aggregate all phase metrics
                if (config.phases.size() > 0) {
                    // Try phases in reverse order, use first non-empty one
                    bool merged = false;
                    for (int phase_idx = config.phases.size(); phase_idx >= 1 && !merged; phase_idx--) {
                        std::string phase_file = output_dir + "/" + test_name + "_phase" +
                                                std::to_string(phase_idx) + ".json";
                        if (fs::exists(phase_file) && fs::file_size(phase_file) > 0) {
                            fs::copy_file(phase_file, output_file, fs::copy_options::overwrite_existing);
                            merged = true;
                            if (verbose) {
                                log("  Merged phase" + std::to_string(phase_idx) + " into combined result");
                            }
                        }
                    }
                    if (!merged) {
                        log("  Warning: No valid phase results to merge for " + test_name);
                    }
                }
            } else {
                // Single-phase workload (legacy behavior)
                std::ostringstream fio_cmd;
                fio_cmd << "fio"
                        << " --name=" << test_name
                        << " --filename=" << test_file
                        << " --size=" << config.file_size
                        << " --runtime=" << config.runtime
                        << " --time_based=1"
                        << " --rw=" << config.pattern
                        << " --bs=" << config.block_size
                        << " --numjobs=" << config.numjobs
                        << " --iodepth=" << config.iodepth;

                if (!config.ioengine.empty()) {
                    fio_cmd << " --ioengine=" << config.ioengine;
                }

                if (config.rate_iops > 0) {
                    fio_cmd << " --rate_iops=" << config.rate_iops;
                }

                fio_cmd << " --group_reporting=1"
                        << " --output-format=json"
                        << " --output=" << output_file
                        << " --status-interval=5";

                if (cache_mode == "direct") {
                    fio_cmd << " --direct=1";
                }

                // Run test
                if (verbose) {
                    log("  Executing: " + fio_cmd.str());
                    run_system(fio_cmd.str());
                } else {
                    std::string silent_cmd = fio_cmd.str() + " >/dev/null 2>&1";
                    run_system(silent_cmd);
                }
            }

            // Check result and log
            if (fs::exists(output_file)) {
                log("  ✓ Completed: " + test_name);
            } else {
                log("  ✗ Failed: " + test_name);
            }

            // Stop iostat
            if (iostat_pid > 0) {
                kill(iostat_pid, SIGTERM);
                waitpid(iostat_pid, nullptr, 0);
            }
            sleep(1);
        }

        return true;
    }

    void run_all_workloads() {
        log("Running all " + std::to_string(workloads.size()) + " fairness workloads...");

        int completed = 0;
        for (const auto& [name, config] : workloads) {
            run_workload(name);
            completed++;
            log("Progress: " + std::to_string(completed) + "/" + std::to_string(workloads.size()) + " workloads completed");
        }
    }

    void generate_summary() {
        int json_files = 0;
        int iostat_files = 0;

        for (const auto& entry : fs::directory_iterator(output_dir)) {
            if (entry.path().extension() == ".json" &&
                entry.path().filename() != "metadata.txt") {
                json_files++;
            }
        }

        for (const auto& entry : fs::directory_iterator(output_dir + "/iostat")) {
            if (entry.path().extension() == ".iostat") {
                iostat_files++;
            }
        }

        log("Generated " + std::to_string(json_files) + " fio results and " +
            std::to_string(iostat_files) + " iostat logs");

        std::ofstream summary(output_dir + "/summary.txt");
        summary << "Fairness Benchmark Results Summary\n"
                << "=================================\n"
                << "Timestamp: " << get_timestamp() << "\n"
                << "Config File: " << config_file << "\n"
                << "\n"
                << "Results:\n"
                << "- FIO JSON results: " << json_files << " files\n"
                << "- iostat monitoring: " << iostat_files << " files\n"
                << "\n"
                << "To analyze results:\n"
                << "    ./quick_fairness_analysis.py " << output_dir << "\n"
                << std::endl;
        summary.close();

        log("Summary saved to " + output_dir + "/summary.txt");
    }

    bool parse_config_file() {
        std::ifstream file(config_file);
        if (!file.is_open()) {
            log("ERROR: Cannot open config file: " + config_file);
            return false;
        }

        std::string line, current_section;
        WorkloadConfig current_workload;
        std::map<int, PhaseConfig> phase_map; // Temporary storage for phases

        while (std::getline(file, line)) {
            // Skip empty lines and comments
            if (line.empty() || line[0] == '#' || line[0] == ';') {
                continue;
            }

            // Section header [workload_name]
            if (line[0] == '[' && line.back() == ']') {
                if (!current_section.empty()) {
                    // Convert phase_map to phases vector
                    for (const auto& [phase_num, phase_config] : phase_map) {
                        current_workload.phases.push_back(phase_config);
                    }
                    workloads[current_section] = current_workload;
                }
                current_section = line.substr(1, line.length() - 2);
                current_workload = WorkloadConfig(); // Reset
                phase_map.clear();
                continue;
            }

            // Key=value pairs
            size_t eq_pos = line.find('=');
            if (eq_pos != std::string::npos) {
                std::string key = line.substr(0, eq_pos);
                std::string value = line.substr(eq_pos + 1);

                // Trim whitespace
                key.erase(0, key.find_first_not_of(" \t"));
                key.erase(key.find_last_not_of(" \t") + 1);
                value.erase(0, value.find_first_not_of(" \t"));
                value.erase(value.find_last_not_of(" \t") + 1);

                // Check for phase-specific parameters (phase_N_*)
                if (key.substr(0, 6) == "phase_") {
                    size_t underscore_pos = key.find('_', 6);
                    if (underscore_pos != std::string::npos) {
                        int phase_num = std::stoi(key.substr(6, underscore_pos - 6));
                        std::string param = key.substr(underscore_pos + 1);

                        // Initialize phase if needed
                        if (phase_map.find(phase_num) == phase_map.end()) {
                            phase_map[phase_num] = PhaseConfig{0, "", 0, "", "", 0};
                        }

                        if (param == "runtime") phase_map[phase_num].runtime = std::stoi(value);
                        else if (param == "block_size") phase_map[phase_num].block_size = value;
                        else if (param == "iodepth") phase_map[phase_num].iodepth = std::stoi(value);
                        else if (param == "pattern") phase_map[phase_num].pattern = value;
                        else if (param == "ioengine") phase_map[phase_num].ioengine = value;
                        else if (param == "rate_iops") phase_map[phase_num].rate_iops = std::stoi(value);
                    }
                }
                // Legacy single-phase parameters
                else if (key == "description") current_workload.description = value;
                else if (key == "file_size") current_workload.file_size = value;
                else if (key == "block_size") current_workload.block_size = value;
                else if (key == "runtime") current_workload.runtime = std::stoi(value);
                else if (key == "numjobs") current_workload.numjobs = std::stoi(value);
                else if (key == "iodepth") current_workload.iodepth = std::stoi(value);
                else if (key == "pattern") current_workload.pattern = value;
                else if (key == "ioengine") current_workload.ioengine = value;
                else if (key == "rate_iops") current_workload.rate_iops = std::stoi(value);
            }
        }

        // Add the last workload
        if (!current_section.empty()) {
            // Convert phase_map to phases vector
            for (const auto& [phase_num, phase_config] : phase_map) {
                current_workload.phases.push_back(phase_config);
            }
            workloads[current_section] = current_workload;
        }

        file.close();
        return !workloads.empty();
    }

public:
    FairnessBenchmark() : config_file("fairness_configs.ini"),
                          output_dir("fairness_results"),
                          verbose(false) {}

    void show_usage(const std::string& program_name) {
        std::cout << "Usage: " << program_name << " [OPTIONS] [WORKLOAD]\n\n"
                  << "Run fairness benchmark tests using fairness_configs.ini\n\n"
                  << "WORKLOADS:\n"
                  << "    steady_reader_d1      Steady 4k reader (iodepth=1) - 1G file\n"
                  << "    steady_reader_d32     Steady 4k reader (iodepth=32) - 1G file\n"
                  << "    steady_writer_d1      Steady 4k writer (iodepth=1) - 1G file\n"
                  << "    steady_writer_d32     Steady 4k writer (iodepth=32) - 1G file\n"
                  << "    bursty_reader_d1      Bursty 4k reader (iodepth=1) - 16G file\n"
                  << "    bursty_reader_d32     Bursty 4k reader (iodepth=32) - 16G file\n"
                  << "    bursty_writer_d1      Bursty 4k writer (iodepth=1) - 16G file\n"
                  << "    bursty_writer_d32     Bursty 4k writer (iodepth=32) - 16G file\n"
                  << "    all                   Run all workloads (default)\n\n"
                  << "OPTIONS:\n"
                  << "    -c, --config FILE     Use custom config file (default: fairness_configs.ini)\n"
                  << "    -o, --output DIR      Output directory (default: fairness_results)\n"
                  << "    -v, --verbose         Verbose output\n"
                  << "    -h, --help            Show this help message\n\n"
                  << "EXAMPLES:\n"
                  << "    " << program_name << "                           # Run all fairness workloads\n"
                  << "    " << program_name << " steady_reader_d1          # Run only steady reader with iodepth=1\n"
                  << "    " << program_name << " -v bursty_writer_d32      # Run bursty writer with verbose output\n";
    }

    bool parse_args(int argc, char* argv[]) {
        std::string workload = "all";

        for (int i = 1; i < argc; i++) {
            std::string arg = argv[i];

            if (arg == "-c" || arg == "--config") {
                if (i + 1 < argc) {
                    config_file = argv[++i];
                } else {
                    log("ERROR: --config requires a filename");
                    return false;
                }
            } else if (arg == "-o" || arg == "--output") {
                if (i + 1 < argc) {
                    output_dir = argv[++i];
                } else {
                    log("ERROR: --output requires a directory");
                    return false;
                }
            } else if (arg == "-v" || arg == "--verbose") {
                verbose = true;
            } else if (arg == "-h" || arg == "--help") {
                show_usage(argv[0]);
                exit(0);
            } else {
                workload = arg;
            }
        }

        return true;
    }

    int run(const std::string& workload) {
        if (!check_dependencies()) {
            return 1;
        }

        if (!parse_config_file()) {
            log("ERROR: Failed to parse config file");
            return 1;
        }

        log("Starting fairness benchmark");
        log("Workload: " + workload + ", Config: " + config_file);

        setup();

        if (workload == "all") {
            run_all_workloads();
        } else {
            if (!run_workload(workload)) {
                return 1;
            }
        }

        generate_summary();

        log("✅ Fairness benchmark completed! Results in: " + output_dir);
        return 0;
    }
};

int main(int argc, char* argv[]) {
    FairnessBenchmark benchmark;

    if (!benchmark.parse_args(argc, argv)) {
        return 1;
    }

    std::string workload = "all";
    if (argc > 1) {
        // Find the workload argument (the one that's not an option)
        for (int i = 1; i < argc; i++) {
            std::string arg = argv[i];
            if (arg[0] != '-' &&
                (i == 1 || (strcmp(argv[i-1], "-c") != 0 && strcmp(argv[i-1], "--config") != 0 &&
                           strcmp(argv[i-1], "-o") != 0 && strcmp(argv[i-1], "--output") != 0))) {
                workload = arg;
                break;
            }
        }
    }

    return benchmark.run(workload);
}