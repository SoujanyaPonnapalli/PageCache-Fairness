#!/usr/bin/env python3

import json
import os
import sys
from pathlib import Path


def load_fio_results(results_dir):
    """Load FIO benchmark results from JSON files."""
    results_path = Path(results_dir)
    json_files = list(results_path.glob("*.json"))
    results = []

    for json_file in json_files:
        try:
            with open(json_file, 'r') as f:
                data = json.load(f)

            if 'jobs' not in data or not data['jobs']:
                continue

            job = data['jobs'][0]
            test_name = json_file.stem

            # Extract metrics
            read_metrics = job.get('read', {})
            write_metrics = job.get('write', {})

            result = {
                'test_name': test_name,
                'file_path': str(json_file),

                # IOPS
                'read_iops': read_metrics.get('iops', 0),
                'write_iops': write_metrics.get('iops', 0),
                'total_iops': read_metrics.get('iops', 0) + write_metrics.get('iops', 0),

                # Bandwidth (MB/s)
                'read_bw_mbs': read_metrics.get('bw_bytes', 0) / 1024 / 1024,
                'write_bw_mbs': write_metrics.get('bw_bytes', 0) / 1024 / 1024,
                'total_bw_mbs': (read_metrics.get('bw_bytes', 0) + write_metrics.get('bw_bytes', 0)) / 1024 / 1024,

                # Latency (microseconds)
                'read_lat_avg_us': read_metrics.get('lat_ns', {}).get('mean', 0) / 1000,
                'write_lat_avg_us': write_metrics.get('lat_ns', {}).get('mean', 0) / 1000,
                'read_lat_p99_us': read_metrics.get('lat_ns', {}).get('percentile', {}).get('99.000000', 0) / 1000,
                'write_lat_p99_us': write_metrics.get('lat_ns', {}).get('percentile', {}).get('99.000000', 0) / 1000,
            }

            results.append(result)

        except (json.JSONDecodeError, KeyError, FileNotFoundError) as e:
            print(f"Warning: Could not parse {json_file}: {e}")

    return results

def analyze_fairness_results(results_dir):
    """Quick analysis of fairness results without external dependencies."""
    results_path = Path(results_dir)

    print("# 🎯 FAIRNESS BENCHMARK ANALYSIS")
    print("=" * 50)

    # Load results
    results = load_fio_results(results_dir)
    if not results:
        print("No results found!")
        return

    print(f"**Total Tests:** {len(results)}")
    print()

    # Group results by workload
    workloads = {}
    for result in results:
        # Parse workload name from test_name
        # Format: workload_name_cached or workload_name_direct
        test_name = result['test_name']
        if test_name.endswith('_cached') or test_name.endswith('_direct'):
            workload_name = test_name.rsplit('_', 1)[0]
            cache_mode = test_name.rsplit('_', 1)[1]
        else:
            continue

        if workload_name not in workloads:
            workloads[workload_name] = {}

        workloads[workload_name][cache_mode] = result

    print("## 📊 WORKLOAD PERFORMANCE COMPARISON")
    print()
    print(f"{'Workload':<20} {'Mode':<8} {'IOPS':<12} {'BW(MB/s)':<10} {'Lat(μs)':<10}")
    print("-" * 65)

    improvements = []

    for workload_name in sorted(workloads.keys()):
        modes = workloads[workload_name]

        if 'cached' in modes and 'direct' in modes:
            cached = modes['cached']
            direct = modes['direct']

            # Use appropriate metrics based on workload type
            if 'reader' in workload_name:
                cached_iops = cached['read_iops']
                cached_bw = cached['read_bw_mbs']
                cached_lat = cached['read_lat_avg_us']
                direct_iops = direct['read_iops']
                direct_bw = direct['read_bw_mbs']
                direct_lat = direct['read_lat_avg_us']
            else:  # writer
                cached_iops = cached['write_iops']
                cached_bw = cached['write_bw_mbs']
                cached_lat = cached['write_lat_avg_us']
                direct_iops = direct['write_iops']
                direct_bw = direct['write_bw_mbs']
                direct_lat = direct['write_lat_avg_us']

            print(f"{workload_name:<20} {'cached':<8} {cached_iops:<12.0f} {cached_bw:<10.1f} {cached_lat:<10.1f}")
            print(f"{'':20} {'direct':<8} {direct_iops:<12.0f} {direct_bw:<10.1f} {direct_lat:<10.1f}")

            # Calculate improvements
            if direct_iops > 0:
                iops_improvement = (cached_iops - direct_iops) / direct_iops * 100
                bw_improvement = (cached_bw - direct_bw) / direct_bw * 100
                lat_improvement = (direct_lat - cached_lat) / direct_lat * 100 if direct_lat > 0 else 0

                print(f"{'':20} {'improve':<8} {iops_improvement:<+12.1f}% {bw_improvement:<+9.1f}% {lat_improvement:<+9.1f}%")

                improvements.append({
                    'workload': workload_name,
                    'iops_imp': iops_improvement,
                    'bw_imp': bw_improvement,
                    'lat_imp': lat_improvement
                })

            print("-" * 65)

    if improvements:
        print()
        print("## 🔍 KEY INSIGHTS")
        print()

        # Category analysis
        steady_improvements = [imp for imp in improvements if 'steady' in imp['workload']]
        bursty_improvements = [imp for imp in improvements if 'bursty' in imp['workload']]
        reader_improvements = [imp for imp in improvements if 'reader' in imp['workload']]
        writer_improvements = [imp for imp in improvements if 'writer' in imp['workload']]
        d1_improvements = [imp for imp in improvements if 'd1' in imp['workload']]
        d32_improvements = [imp for imp in improvements if 'd32' in imp['workload']]

        print("### By Workload Type:")
        if steady_improvements:
            avg_steady = sum(imp['iops_imp'] for imp in steady_improvements) / len(steady_improvements)
            print(f"- **Steady (1G file):** {avg_steady:+.1f}% average IOPS improvement")

        if bursty_improvements:
            avg_bursty = sum(imp['iops_imp'] for imp in bursty_improvements) / len(bursty_improvements)
            print(f"- **Bursty (16G file):** {avg_bursty:+.1f}% average IOPS improvement")

        print()
        print("### By I/O Pattern:")
        if reader_improvements:
            avg_read = sum(imp['iops_imp'] for imp in reader_improvements) / len(reader_improvements)
            print(f"- **Readers:** {avg_read:+.1f}% average IOPS improvement")

        if writer_improvements:
            avg_write = sum(imp['iops_imp'] for imp in writer_improvements) / len(writer_improvements)
            print(f"- **Writers:** {avg_write:+.1f}% average IOPS improvement")

        print()
        print("### By I/O Depth:")
        if d1_improvements:
            avg_d1 = sum(imp['iops_imp'] for imp in d1_improvements) / len(d1_improvements)
            print(f"- **Depth=1:** {avg_d1:+.1f}% average IOPS improvement")

        if d32_improvements:
            avg_d32 = sum(imp['iops_imp'] for imp in d32_improvements) / len(d32_improvements)
            print(f"- **Depth=32:** {avg_d32:+.1f}% average IOPS improvement")

        # Best and worst
        best = max(improvements, key=lambda x: x['iops_imp'])
        worst = min(improvements, key=lambda x: x['iops_imp'])

        print()
        print("### Performance Extremes:")
        print(f"- **Best pagecache benefit:** {best['workload']} ({best['iops_imp']:+.1f}% IOPS)")
        print(f"- **Least pagecache benefit:** {worst['workload']} ({worst['iops_imp']:+.1f}% IOPS)")

        overall_avg = sum(imp['iops_imp'] for imp in improvements) / len(improvements)
        print(f"- **Overall average:** {overall_avg:+.1f}% IOPS improvement")

    print()
    print("## 💾 DETAILED WORKLOAD BREAKDOWN")
    print()

    for workload_name in sorted(workloads.keys()):
        modes = workloads[workload_name]
        print(f"### {workload_name}")

        # Parse workload characteristics
        if 'steady' in workload_name:
            print("- **Type:** Sequential I/O on small file (1G)")
        elif 'bursty' in workload_name:
            print("- **Type:** Random I/O on large file (16G)")

        if 'reader' in workload_name:
            print("- **Pattern:** Read operations")
        elif 'writer' in workload_name:
            print("- **Pattern:** Write operations")

        if 'd1' in workload_name:
            print("- **Concurrency:** Low (iodepth=1)")
        elif 'd32' in workload_name:
            print("- **Concurrency:** High (iodepth=32)")

        if 'cached' in modes and 'direct' in modes:
            cached = modes['cached']
            direct = modes['direct']

            if 'reader' in workload_name:
                print(f"- **Cached Results:** {cached['read_iops']:.0f} IOPS, {cached['read_bw_mbs']:.1f} MB/s, {cached['read_lat_avg_us']:.1f}μs")
                print(f"- **Direct Results:** {direct['read_iops']:.0f} IOPS, {direct['read_bw_mbs']:.1f} MB/s, {direct['read_lat_avg_us']:.1f}μs")
            else:
                print(f"- **Cached Results:** {cached['write_iops']:.0f} IOPS, {cached['write_bw_mbs']:.1f} MB/s, {cached['write_lat_avg_us']:.1f}μs")
                print(f"- **Direct Results:** {direct['write_iops']:.0f} IOPS, {direct['write_bw_mbs']:.1f} MB/s, {direct['write_lat_avg_us']:.1f}μs")

        print()

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 quick_fairness_analysis.py <results_directory>")
        sys.exit(1)

    results_dir = sys.argv[1]
    analyze_fairness_results(results_dir)

if __name__ == '__main__':
    main()