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
};

struct WorkloadConfig {
    std::string description;
    std::string file_size;
    int numjobs;
    // Legacy single-phase config (for backward compatibility)
    std::string block_size;
    int runtime;
    int iodepth;
    std::string pattern;
    std::string ioengine;
    // Multi-phase config
    std::vector<PhaseConfig> phases;
};

struct CgroupConfig {
    std::string cgroup_name;
    std::map<std::string, std::string> settings;
};

class FairnessBenchmark {
private:
    std::string config_file;
    std::string output_dir;
    bool verbose;
    std::map<std::string, WorkloadConfig> workloads;
    std::map<std::string, CgroupConfig> cgroups;
    std::string cgroup_config_file;
    bool use_cgroups;

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
        system("sync");
        system("sudo purge 2>/dev/null || true");
        sleep(1);
    }

    bool parse_cgroup_config() {
        if (!fs::exists(cgroup_config_file)) {
            log("Cgroup config file not found: " + cgroup_config_file + ", skipping cgroups");
            use_cgroups = false;
            return true;
        }

        std::ifstream file(cgroup_config_file);
        if (!file.is_open()) {
            log("WARNING: Could not open cgroup config file, skipping cgroups");
            use_cgroups = false;
            return true;
        }

        std::string line;
        std::string current_client;
        CgroupConfig current_cgroup;

        while (std::getline(file, line)) {
            line.erase(0, line.find_first_not_of(" \t"));
            line.erase(line.find_last_not_of(" \t\r\n") + 1);

            if (line.empty() || line[0] == '#') continue;

            if (line[0] == '[') {
                if (!current_client.empty()) {
                    cgroups[current_client] = current_cgroup;
                }
                current_client = line.substr(1, line.find(']') - 1);
                current_cgroup = CgroupConfig();
                current_cgroup.settings.clear();
                continue;
            }

            size_t eq_pos = line.find('=');
            if (eq_pos != std::string::npos) {
                std::string key = line.substr(0, eq_pos);
                std::string value = line.substr(eq_pos + 1);
                key.erase(key.find_last_not_of(" \t") + 1);
                value.erase(0, value.find_first_not_of(" \t"));

                if (key == "cgroup_name") {
                    current_cgroup.cgroup_name = value;
                } else {
                    current_cgroup.settings[key] = value;
                }
            }
        }

        if (!current_client.empty()) {
            cgroups[current_client] = current_cgroup;
        }

        file.close();
        log("Loaded cgroup config for " + std::to_string(cgroups.size()) + " clients");
        return true;
    }

    bool setup_cgroup(const std::string& client_name) {
        if (!use_cgroups) return true;

        auto it = cgroups.find(client_name);
        if (it == cgroups.end()) {
            log("WARNING: No cgroup config for " + client_name + ", running without cgroup");
            return true;
        }

        const auto& cgroup = it->second;
        std::string cgroup_path = "/sys/fs/cgroup/" + cgroup.cgroup_name;

        // Create cgroup directory
        std::string mkdir_cmd = "sudo mkdir -p " + cgroup_path + " 2>/dev/null";
        if (system(mkdir_cmd.c_str()) != 0) {
            log("WARNING: Failed to create cgroup " + cgroup_path + ", running without cgroup");
            return true;
        }

        // Enable controllers in parent cgroup first
        // This is required for cgroup v2
        std::string enable_controllers = "echo '+cpu +memory +io' | sudo tee /sys/fs/cgroup/cgroup.subtree_control > /dev/null 2>&1";
        system(enable_controllers.c_str());

        // Apply cgroup settings
        int success_count = 0;
        int fail_count = 0;

        for (const auto& [key, value] : cgroup.settings) {
            std::string setting_file = cgroup_path + "/" + key;

            // Check if the controller file exists first
            std::string check_cmd = "test -f " + setting_file;
            if (system(check_cmd.c_str()) != 0) {
                // File doesn't exist, controller might not be enabled
                continue;
            }

            std::string set_cmd = "echo '" + value + "' | sudo tee " + setting_file + " > /dev/null 2>&1";
            if (system(set_cmd.c_str()) == 0) {
                success_count++;
            } else {
                fail_count++;
                if (verbose) {
                    log("WARNING: Failed to set " + key + " for cgroup " + cgroup.cgroup_name);
                }
            }
        }

        if (success_count > 0) {
            log("Setup cgroup: " + cgroup.cgroup_name + " (" + std::to_string(success_count) +
                " settings applied, " + std::to_string(fail_count) + " failed)");
        } else {
            log("WARNING: No cgroup settings applied for " + cgroup.cgroup_name + " (controllers may not be available)");
        }

        return true;
    }

    bool add_pid_to_cgroup(const std::string& client_name, pid_t pid) {
        if (!use_cgroups) return true;

        auto it = cgroups.find(client_name);
        if (it == cgroups.end()) return true;

        const auto& cgroup = it->second;
        std::string cgroup_path = "/sys/fs/cgroup/" + cgroup.cgroup_name;
        std::string procs_file = cgroup_path + "/cgroup.procs";

        std::string add_cmd = "echo " + std::to_string(pid) + " | sudo tee " + procs_file + " > /dev/null 2>&1";
        if (system(add_cmd.c_str()) != 0) {
            log("WARNING: Failed to add PID " + std::to_string(pid) + " to cgroup " + cgroup.cgroup_name);
            return false;
        }

        return true;
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
        system(cmd.c_str());
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
                freopen(iostat_file.c_str(), "w", stdout);
                freopen("/dev/null", "w", stderr);
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

                    log("    Phase " + std::to_string(phase_idx + 1) + "/" + std::to_string(config.phases.size()) +
                        ": " + phase.pattern + " for " + std::to_string(phase.runtime) + "s");

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
                        system(fio_cmd.str().c_str());
                    } else {
                        std::string silent_cmd = fio_cmd.str() + " >/dev/null 2>&1";
                        system(silent_cmd.c_str());
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
                    system(fio_cmd.str().c_str());
                } else {
                    std::string silent_cmd = fio_cmd.str() + " >/dev/null 2>&1";
                    system(silent_cmd.c_str());
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

    bool run_concurrent_clients() {
        // Find client1 and client2 in workloads
        auto client1_it = workloads.find("client1_steady");
        auto client2_it = workloads.find("client2_bursty");

        if (client1_it == workloads.end() || client2_it == workloads.end()) {
            log("ERROR: Dual-client mode requires 'client1_steady' and 'client2_bursty' in config");
            return false;
        }

        log("Starting concurrent dual-client fairness test");
        log("Client1 (steady): " + client1_it->second.description);
        log("Client2 (bursty): " + client2_it->second.description);

        // Setup cgroups for both clients
        setup_cgroup("client1_steady");
        setup_cgroup("client2_bursty");

        // Create test files for both clients
        std::string script_dir = fs::current_path().string();
        std::string client1_file = script_dir + "/test_file_" + client1_it->second.file_size;
        std::string client2_file = script_dir + "/test_file_" + client2_it->second.file_size;

        create_test_file(client1_it->second.file_size, client1_file);
        create_test_file(client2_it->second.file_size, client2_file);

        // Test both cached and direct modes
        std::vector<std::string> cache_modes = {"cached", "direct"};
        for (const auto& cache_mode : cache_modes) {
            log("Running mode: " + cache_mode);

            // Start iostat monitoring
            std::string iostat_file = output_dir + "/iostat/concurrent_" + cache_mode + ".iostat";
            pid_t iostat_pid = fork();
            if (iostat_pid == 0) {
                freopen(iostat_file.c_str(), "w", stdout);
                freopen("/dev/null", "w", stderr);
                execl("/usr/bin/iostat", "iostat", "-d", "-w", "1", nullptr);
                exit(1);
            }

            drop_caches();

            // Spawn both clients concurrently
            std::vector<pid_t> client_pids;

            // Launch client1
            pid_t client1_pid = fork();
            if (client1_pid == 0) {
                // Add self to cgroup
                add_pid_to_cgroup("client1_steady", getpid());
                run_client_process("client1", client1_it->second, client1_file, cache_mode);
                exit(0);
            }
            client_pids.push_back(client1_pid);
            // Add child to cgroup from parent side
            add_pid_to_cgroup("client1_steady", client1_pid);

            // Launch client2
            pid_t client2_pid = fork();
            if (client2_pid == 0) {
                // Add self to cgroup
                add_pid_to_cgroup("client2_bursty", getpid());
                run_client_process("client2", client2_it->second, client2_file, cache_mode);
                exit(0);
            }
            client_pids.push_back(client2_pid);
            // Add child to cgroup from parent side
            add_pid_to_cgroup("client2_bursty", client2_pid);

            // Wait for both clients to complete
            for (pid_t pid : client_pids) {
                int status;
                waitpid(pid, &status, 0);
                if (WIFEXITED(status) && WEXITSTATUS(status) == 0) {
                    log("  ✓ Client completed successfully");
                } else {
                    log("  ✗ Client failed or was terminated");
                }
            }

            // Stop iostat
            if (iostat_pid > 0) {
                kill(iostat_pid, SIGTERM);
                waitpid(iostat_pid, nullptr, 0);
            }

            log("Completed mode: " + cache_mode);
            sleep(2);
        }

        return true;
    }

    void run_client_process(const std::string& client_name, const WorkloadConfig& config,
                           const std::string& test_file, const std::string& cache_mode) {
        // Run all phases for this client
        for (size_t phase_idx = 0; phase_idx < config.phases.size(); phase_idx++) {
            const auto& phase = config.phases[phase_idx];
            std::string phase_name = client_name + "_" + cache_mode + "_phase" + std::to_string(phase_idx + 1);
            std::string phase_output = output_dir + "/" + phase_name + ".json";
            std::string log_prefix = output_dir + "/" + phase_name;

            // Build fio command with per-second logging
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

            // Add per-second logging
            fio_cmd << " --log_avg_msec=1000"
                    << " --write_lat_log=" << log_prefix
                    << " --write_bw_log=" << log_prefix
                    << " --write_iops_log=" << log_prefix;

            fio_cmd << " --group_reporting=1"
                    << " --output-format=json"
                    << " --output=" << phase_output;

            if (cache_mode == "direct") {
                fio_cmd << " --direct=1";
            }

            // Execute fio
            system(fio_cmd.str().c_str());
        }
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
                            phase_map[phase_num] = PhaseConfig{0, "", 0, "", ""};
                        }

                        if (param == "runtime") phase_map[phase_num].runtime = std::stoi(value);
                        else if (param == "block_size") phase_map[phase_num].block_size = value;
                        else if (param == "iodepth") phase_map[phase_num].iodepth = std::stoi(value);
                        else if (param == "pattern") phase_map[phase_num].pattern = value;
                        else if (param == "ioengine") phase_map[phase_num].ioengine = value;
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
                          verbose(false),
                          cgroup_config_file("cgroup_config.ini"),
                          use_cgroups(true) {}

    void show_usage(const std::string& program_name) {
        std::cout << "Usage: " << program_name << " [OPTIONS] [MODE]\n\n"
                  << "Run fairness benchmark tests using fairness_configs.ini\n\n"
                  << "MODES:\n"
                  << "    dual                  Run concurrent dual-client fairness test (default)\n"
                  << "    all                   Run all sequential workloads\n"
                  << "    <workload_name>       Run specific workload\n\n"
                  << "OPTIONS:\n"
                  << "    -c, --config FILE     Use custom config file (default: fairness_configs.ini)\n"
                  << "    -o, --output DIR      Output directory (default: fairness_results)\n"
                  << "    -v, --verbose         Verbose output\n"
                  << "    -h, --help            Show this help message\n\n"
                  << "DUAL-CLIENT MODE:\n"
                  << "    Runs client1_steady and client2_bursty concurrently\n"
                  << "    Logs per-second IOPS, bandwidth, and latency\n"
                  << "    Monitors system I/O with iostat at 1-second intervals\n\n"
                  << "EXAMPLES:\n"
                  << "    " << program_name << "                           # Run dual-client fairness test\n"
                  << "    " << program_name << " dual                      # Run dual-client fairness test\n"
                  << "    " << program_name << " -v dual                   # Run dual-client with verbose output\n";
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

    int run(const std::string& mode) {
        if (!check_dependencies()) {
            return 1;
        }

        if (!parse_config_file()) {
            log("ERROR: Failed to parse config file");
            return 1;
        }

        // Parse cgroup configuration
        parse_cgroup_config();

        log("Starting fairness benchmark");
        log("Mode: " + mode + ", Config: " + config_file);

        setup();

        // Check if config has dual-client setup
        bool has_dual_clients = (workloads.find("client1_steady") != workloads.end() &&
                                 workloads.find("client2_bursty") != workloads.end());

        if (mode == "dual") {
            if (!has_dual_clients) {
                log("ERROR: Dual-client mode requires 'client1_steady' and 'client2_bursty' in config");
                return 1;
            }
            if (!run_concurrent_clients()) {
                return 1;
            }
        } else if (mode == "all") {
            run_all_workloads();
        } else {
            if (!run_workload(mode)) {
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

    std::string mode = "dual";  // Default to dual-client mode
    if (argc > 1) {
        // Find the mode argument (the one that's not an option)
        for (int i = 1; i < argc; i++) {
            std::string arg = argv[i];
            if (arg[0] != '-' &&
                (i == 1 || (strcmp(argv[i-1], "-c") != 0 && strcmp(argv[i-1], "--config") != 0 &&
                           strcmp(argv[i-1], "-o") != 0 && strcmp(argv[i-1], "--output") != 0))) {
                mode = arg;
                break;
            }
        }
    }

    return benchmark.run(mode);
}