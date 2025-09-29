#!/bin/bash
# Diagnostic script to check cgroup v2 setup

echo "=== Cgroup Setup Diagnostics ==="
echo

# Check if cgroup v2 is mounted
echo "1. Checking cgroup v2 mount:"
if mount | grep -q "cgroup2 on /sys/fs/cgroup"; then
    echo "   ✓ cgroup v2 is mounted at /sys/fs/cgroup"
else
    echo "   ✗ cgroup v2 not mounted properly"
    echo "   Current mount:"
    mount | grep cgroup
fi
echo

# Check available controllers at root
echo "2. Available controllers at root:"
if [ -f /sys/fs/cgroup/cgroup.controllers ]; then
    cat /sys/fs/cgroup/cgroup.controllers
else
    echo "   ✗ /sys/fs/cgroup/cgroup.controllers not found"
fi
echo

# Check enabled controllers for subtree
echo "3. Enabled subtree controllers:"
if [ -f /sys/fs/cgroup/cgroup.subtree_control ]; then
    cat /sys/fs/cgroup/cgroup.subtree_control
    if [ -z "$(cat /sys/fs/cgroup/cgroup.subtree_control)" ]; then
        echo "   ⚠️  No controllers enabled (empty)"
    fi
else
    echo "   ✗ /sys/fs/cgroup/cgroup.subtree_control not found"
fi
echo

# Check if we're running with proper permissions
echo "4. Permission check:"
if [ "$(id -u)" -eq 0 ]; then
    echo "   ✓ Running as root"
elif groups | grep -qw sudo; then
    echo "   ✓ User is in sudo group"
else
    echo "   ⚠️  User not in sudo group, may need elevated permissions"
fi
echo

# Try to enable controllers
echo "5. Attempting to enable controllers (requires sudo):"
if sudo echo '+cpu +memory +io' > /sys/fs/cgroup/cgroup.subtree_control 2>/dev/null; then
    echo "   ✓ Successfully enabled cpu, memory, io controllers"
else
    echo "   ✗ Failed to enable controllers"
    echo "   This is normal if already enabled or if systemd manages cgroups"
fi
echo

# Check specific controller files
echo "6. Checking controller availability:"
for ctrl in cpu.weight cpu.max memory.high memory.max memory.swap.max io.weight; do
    if [ -f "/sys/fs/cgroup/$ctrl" ]; then
        echo "   ✓ $ctrl exists"
    else
        echo "   ✗ $ctrl NOT found"
    fi
done
echo

# Check if systemd is managing cgroups
echo "7. Systemd cgroup management:"
if systemctl --version &>/dev/null; then
    echo "   ✓ systemd is present"
    if [ -d /sys/fs/cgroup/system.slice ]; then
        echo "   ✓ systemd is managing cgroups"
        echo "   NOTE: With systemd, use 'systemd-run' or modify /etc/systemd/system.conf"
    fi
else
    echo "   ✗ systemd not detected"
fi
echo

# Recommendations
echo "=== Recommendations ==="
echo
if [ ! -f /sys/fs/cgroup/cgroup.subtree_control ]; then
    echo "⚠️  cgroup v2 not properly set up"
    echo "   - Ensure your kernel supports unified cgroup hierarchy (cgroup v2)"
    echo "   - Boot with: cgroup_no_v1=all systemd.unified_cgroup_hierarchy=1"
elif [ -z "$(cat /sys/fs/cgroup/cgroup.subtree_control 2>/dev/null)" ]; then
    echo "⚠️  Controllers not enabled"
    echo "   - Run: echo '+cpu +memory +io' | sudo tee /sys/fs/cgroup/cgroup.subtree_control"
    echo "   - Or use systemd-run to launch benchmark in a scope with controllers"
else
    echo "✓ Basic cgroup v2 setup looks good"
    echo
    echo "To use cgroups with this benchmark:"
    echo "  1. Run as root: sudo ./fairness_benchmark dual"
    echo "  2. Or use systemd-run:"
    echo "     systemd-run --scope --unit=fairness-test ./fairness_benchmark dual"
fi
echo