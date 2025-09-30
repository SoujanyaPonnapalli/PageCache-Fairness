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
                content = f.read()

            # Handle multiple JSON objects in one file
            json_objects = []
            decoder = json.JSONDecoder()
            idx = 0
            content_to_parse = content
            while idx < len(content_to_parse):
                remaining = content_to_parse[idx:].lstrip()
                if not remaining:
                    break
                try:
                    obj, end_idx = decoder.raw_decode(remaining)
                    if isinstance(obj, dict):
                        json_objects.append(obj)
                    idx += len(content_to_parse[idx:]) - len(remaining) + end_idx
                except json.JSONDecodeError:
                    break

            # Use the last JSON object (most recent run)
            if not json_objects:
                continue

            data = json_objects[-1]

            if not isinstance(data, dict) or 'jobs' not in data or not data['jobs']:
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
                'iops_min': read_metrics.get('iops_min', 0) if read_metrics.get('iops', 0) > 0 else write_metrics.get('iops_min', 0),
                'iops_max': read_metrics.get('iops_max', 0) if read_metrics.get('iops', 0) > 0 else write_metrics.get('iops_max', 0),
                'iops_stddev': read_metrics.get('iops_stddev', 0) if read_metrics.get('iops', 0) > 0 else write_metrics.get('iops_stddev', 0),

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


def get_iops(result):
    """Helper: Get IOPS from result (read or write)."""
    return result['read_iops'] if result['read_iops'] > 0 else result['write_iops']


def get_bw(result):
    """Helper: Get bandwidth from result (read or write)."""
    return result['read_bw_mbs'] if result['read_iops'] > 0 else result['write_bw_mbs']


def get_lat(result):
    """Helper: Get latency from result (read or write)."""
    return result['read_lat_avg_us'] if result['read_iops'] > 0 else result['write_lat_avg_us']


def get_lat_p99(result):
    """Helper: Get p99 latency from result (read or write)."""
    return result['read_lat_p99_us'] if result['read_iops'] > 0 else result['write_lat_p99_us']


def calculate_average_iops(phases):
    """Helper: Calculate average IOPS across all phases."""
    total = sum(get_iops(p) for p in phases.values())
    return total / len(phases) if phases else 0


def print_phase_metrics(result, label):
    """Helper: Print metrics for a phase result."""
    iops = get_iops(result)
    bw = get_bw(result)
    lat = get_lat(result)
    lat_p99 = get_lat_p99(result)
    print(f"- {label:7s} {iops:>10.0f} IOPS, {bw:>7.1f} MB/s, {lat:>7.1f}μs avg, {lat_p99:>7.1f}μs p99")


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
    phase_results = {}

    for result in results:
        test_name = result['test_name']

        # Check if this is a phase result
        if '_phase' in test_name:
            # Extract: workload_name_cached_phase1 -> workload_name, cached, phase1
            parts = test_name.split('_')
            phase_num = parts[-1]  # phase1, phase2, etc.
            cache_mode = parts[-2]  # cached or direct
            workload_name = '_'.join(parts[:-2])  # everything before cache_mode_phaseN

            if workload_name not in phase_results:
                phase_results[workload_name] = {}
            if cache_mode not in phase_results[workload_name]:
                phase_results[workload_name][cache_mode] = {}
            phase_results[workload_name][cache_mode][phase_num] = result
            continue

        # Regular workload (not phase)
        if test_name.endswith('_cached') or test_name.endswith('_direct'):
            workload_name = test_name.rsplit('_', 1)[0]
            cache_mode = test_name.rsplit('_', 1)[1]
        else:
            continue

        if workload_name not in workloads:
            workloads[workload_name] = {}

        workloads[workload_name][cache_mode] = result

    # Only show workload comparison if non-phase workloads exist
    if workloads:
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

                cached_iops = get_iops(cached)
                cached_bw = get_bw(cached)
                cached_lat = get_lat(cached)
                direct_iops = get_iops(direct)
                direct_bw = get_bw(direct)
                direct_lat = get_lat(direct)

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

    # Add phase-by-phase analysis if any multi-phase workloads exist
    if phase_results:
        print()
        print("## 🔄 MULTI-PHASE WORKLOAD ANALYSIS")
        print()
        for workload_name in sorted(phase_results.keys()):
            print(f"### {workload_name} (Phase-by-Phase)")
            print()
            phases = phase_results[workload_name]

            # Get all phase numbers
            all_phases = set()
            if 'cached' in phases:
                all_phases.update(phases['cached'].keys())
            if 'direct' in phases:
                all_phases.update(phases['direct'].keys())

            for phase_num in sorted(all_phases):
                print(f"**{phase_num.upper()}:**")

                if 'cached' in phases and phase_num in phases['cached']:
                    print_phase_metrics(phases['cached'][phase_num], "Cached:")

                if 'direct' in phases and phase_num in phases['direct']:
                    print_phase_metrics(phases['direct'][phase_num], "Direct:")

                # Calculate improvement for this phase
                if ('cached' in phases and phase_num in phases['cached'] and
                    'direct' in phases and phase_num in phases['direct']):
                    cached_iops = get_iops(phases['cached'][phase_num])
                    direct_iops = get_iops(phases['direct'][phase_num])

                    if direct_iops > 0:
                        improvement = (cached_iops - direct_iops) / direct_iops * 100
                        print(f"- Improvement: {improvement:+.1f}%")

                print()

    # Add comprehensive fairness and stability analysis for dual-client mode
    if phase_results and 'client1' in phase_results and 'client2' in phase_results:
        print()
        print("## ⚖️  DUAL-CLIENT FAIRNESS & STABILITY ANALYSIS")
        print("=" * 75)

        # Phase stability analysis
        print("\n### Phase Stability Analysis")
        print("-" * 75)

        stability_summary = []

        for client_name in ['client1', 'client2']:
            if client_name not in phase_results:
                continue

            client_phases = phase_results[client_name]

            for cache_mode in ['cached', 'direct']:
                if cache_mode not in client_phases:
                    continue

                phases = client_phases[cache_mode]
                phase_keys = sorted(phases.keys())

                if len(phase_keys) < 2:
                    continue

                # Get IOPS for first and last phase
                p1_iops = get_iops(phases[phase_keys[0]])
                p2_iops = get_iops(phases[phase_keys[-1]])

                if p1_iops > 0:
                    change_pct = ((p2_iops - p1_iops) / p1_iops) * 100
                    stability_summary.append({
                        'name': f"{client_name.title()} {cache_mode.title()}",
                        'change': change_pct,
                        'abs_change': abs(change_pct),
                        'p1_iops': p1_iops,
                        'p2_iops': p2_iops
                    })

                    print(f"\n**{client_name.upper()} {cache_mode.upper()}:**")
                    print(f"  Phase1: {p1_iops:>12,.0f} IOPS")
                    print(f"  Phase2: {p2_iops:>12,.0f} IOPS")
                    print(f"  Change: {change_pct:>12.2f}%")

                    if abs(change_pct) < 1:
                        print(f"  ✅ Excellent stability (< 1% variation)")
                    elif abs(change_pct) < 5:
                        print(f"  ✓ Good stability (< 5% variation)")
                    else:
                        print(f"  ⚠️  Poor stability (> 5% variation)")

        # Stability ranking
        if stability_summary:
            print("\n### Stability Ranking (Most Unstable → Most Stable)")
            print("-" * 75)
            stability_summary.sort(key=lambda x: x['abs_change'], reverse=True)

            for i, item in enumerate(stability_summary, 1):
                status = "⚠️ " if item['abs_change'] > 5 else "✓" if item['abs_change'] > 1 else "✅"
                print(f"{i}. {status} {item['name']:25s}: {item['change']:+7.2f}%")

        # Client comparison (fairness)
        print("\n\n### Client Fairness Comparison")
        print("-" * 75)

        for cache_mode in ['cached', 'direct']:
            if ('client1' in phase_results and cache_mode in phase_results['client1'] and
                'client2' in phase_results and cache_mode in phase_results['client2']):

                c1_avg = calculate_average_iops(phase_results['client1'][cache_mode])
                c2_avg = calculate_average_iops(phase_results['client2'][cache_mode])

                if c2_avg > 0:
                    ratio = c1_avg / c2_avg

                    print(f"\n**{cache_mode.upper()} MODE:**")
                    print(f"  Client1 avg: {c1_avg:>12,.0f} IOPS")
                    print(f"  Client2 avg: {c2_avg:>12,.0f} IOPS")
                    print(f"  Ratio:       {ratio:>12.2f}x")

                    if abs(ratio - 1.0) < 0.1:
                        print(f"  ✅ Excellent fairness (~1:1 ratio)")
                    elif abs(ratio - 1.0) < 0.5:
                        print(f"  ✓ Good fairness")
                    else:
                        print(f"  ⚠️  Unfair resource distribution")

        # Variance analysis (intra-phase stability)
        print("\n\n### Intra-Phase Variance Analysis")
        print("-" * 75)
        print("Measures performance consistency WITHIN each 30-second phase\n")

        for client_name in ['client1', 'client2']:
            if client_name not in phase_results:
                continue

            client_phases = phase_results[client_name]
            print(f"\n**{client_name.upper()}:**")

            for cache_mode in ['cached', 'direct']:
                if cache_mode not in client_phases:
                    continue

                print(f"  {cache_mode.title()}:")

                for phase_key in sorted(client_phases[cache_mode].keys()):
                    phase = client_phases[cache_mode][phase_key]
                    iops = get_iops(phase)
                    iops_stddev = phase['iops_stddev']

                    if iops > 0:
                        cv = (iops_stddev / iops) * 100  # Coefficient of variation
                        print(f"    {phase_key}: CoV = {cv:>5.2f}% (σ={iops_stddev:>8,.0f} IOPS)")

        # Pagecache benefit comparison
        print("\n\n### Pagecache Benefit Analysis")
        print("-" * 75)

        for client_name in ['client1', 'client2']:
            if client_name not in phase_results:
                continue

            if 'cached' not in phase_results[client_name] or 'direct' not in phase_results[client_name]:
                continue

            cached_avg = calculate_average_iops(phase_results[client_name]['cached'])
            direct_avg = calculate_average_iops(phase_results[client_name]['direct'])

            if direct_avg > 0:
                benefit_pct = ((cached_avg - direct_avg) / direct_avg) * 100

                print(f"\n**{client_name.upper()}:**")
                print(f"  Cached avg:  {cached_avg:>12,.0f} IOPS")
                print(f"  Direct avg:  {direct_avg:>12,.0f} IOPS")
                print(f"  Benefit:     {benefit_pct:>12.2f}%")

                if benefit_pct < -10:
                    print(f"  ⚠️  Pagecache HURTS performance significantly")
                elif benefit_pct < 0:
                    print(f"  ⚠️  Pagecache slightly degrades performance")
                elif benefit_pct < 10:
                    print(f"  ✓ Modest pagecache benefit")
                else:
                    print(f"  ✅ Strong pagecache benefit")

    print()


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 quick_fairness_analysis.py <results_directory>")
        sys.exit(1)

    results_dir = sys.argv[1]
    analyze_fairness_results(results_dir)


if __name__ == '__main__':
    main()