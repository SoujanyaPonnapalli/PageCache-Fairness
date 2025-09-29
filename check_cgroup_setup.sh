#!/bin/bash
# Comprehensive diagnostic script to check cgroup v2 setup and test actual controller availability

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
    echo "   $(cat /sys/fs/cgroup/cgroup.controllers)"
else
    echo "   ✗ /sys/fs/cgroup/cgroup.controllers not found"
fi
echo

# Check enabled controllers for subtree
echo "3. Enabled subtree controllers:"
if [ -f /sys/fs/cgroup/cgroup.subtree_control ]; then
    SUBTREE_CTRL=$(cat /sys/fs/cgroup/cgroup.subtree_control)
    if [ -z "$SUBTREE_CTRL" ]; then
        echo "   ⚠️  No controllers enabled at root (empty)"
    else
        echo "   $SUBTREE_CTRL"
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
    if sudo -n true 2>/dev/null; then
        echo "   ✓ Passwordless sudo available"
    else
        echo "   ⚠️  Sudo requires password"
    fi
else
    echo "   ⚠️  User not in sudo group, may need elevated permissions"
fi
echo

# Check if systemd is managing cgroups
echo "5. Systemd cgroup management:"
SYSTEMD_MANAGED=false
if systemctl --version &>/dev/null; then
    echo "   ✓ systemd is present"
    if [ -d /sys/fs/cgroup/system.slice ]; then
        echo "   ✓ systemd is managing cgroups"
        SYSTEMD_MANAGED=true
    fi
else
    echo "   ✗ systemd not detected"
fi
echo

# Check user.slice subtree control if systemd is managing
if [ "$SYSTEMD_MANAGED" = true ]; then
    echo "6. User.slice controller delegation:"
    if [ -f /sys/fs/cgroup/user.slice/cgroup.subtree_control ]; then
        USER_SLICE_CTRL=$(cat /sys/fs/cgroup/user.slice/cgroup.subtree_control)
        if [ -z "$USER_SLICE_CTRL" ]; then
            echo "   ⚠️  No controllers delegated to user.slice (empty)"
        else
            echo "   Delegated: $USER_SLICE_CTRL"
        fi
    else
        echo "   ✗ /sys/fs/cgroup/user.slice/cgroup.subtree_control not found"
    fi
    echo
fi

# Test actual controller availability by creating a test cgroup
echo "7. Testing Controller Availability (following cgroup_config.ini):"
echo "   Creating test cgroup to check which settings work..."
echo

TEST_CGROUP_PATH="/sys/fs/cgroup/user.slice/test_fairness_diagnostic"
if [ "$SYSTEMD_MANAGED" = false ]; then
    TEST_CGROUP_PATH="/sys/fs/cgroup/test_fairness_diagnostic"
fi

# Create test cgroup
if sudo mkdir -p "$TEST_CGROUP_PATH" 2>/dev/null; then
    echo "   ✓ Created test cgroup: $TEST_CGROUP_PATH"
    echo

    # Try to enable controllers in parent
    PARENT_PATH=$(dirname "$TEST_CGROUP_PATH")
    echo "   Attempting to enable controllers in parent ($PARENT_PATH)..."
    if sudo sh -c "echo '+cpu +memory +io' > $PARENT_PATH/cgroup.subtree_control" 2>/dev/null; then
        echo "   ✓ Enabled controllers in parent"
    else
        echo "   ⚠️  Could not enable all controllers (may be systemd-managed)"
    fi
    echo

    # Test each setting from cgroup_config.ini
    echo "   Testing settings from cgroup_config.ini:"
    echo "   ----------------------------------------"

    SUCCESS_COUNT=0
    FAIL_COUNT=0

    # Test cpu.weight = 100
    if [ -f "$TEST_CGROUP_PATH/cpu.weight" ]; then
        if echo "100" | sudo tee "$TEST_CGROUP_PATH/cpu.weight" > /dev/null 2>&1; then
            echo "   ✅ cpu.weight = 100 (SUCCESS)"
            SUCCESS_COUNT=$((SUCCESS_COUNT + 1))
        else
            echo "   ✗ cpu.weight = 100 (FAILED - permission denied)"
            FAIL_COUNT=$((FAIL_COUNT + 1))
        fi
    else
        echo "   ✗ cpu.weight (FAILED - file not found, controller not available)"
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi

    # Test cpu.max = 100000 100000
    if [ -f "$TEST_CGROUP_PATH/cpu.max" ]; then
        if echo "100000 100000" | sudo tee "$TEST_CGROUP_PATH/cpu.max" > /dev/null 2>&1; then
            echo "   ✅ cpu.max = 100000 100000 (SUCCESS)"
            SUCCESS_COUNT=$((SUCCESS_COUNT + 1))
        else
            echo "   ✗ cpu.max = 100000 100000 (FAILED - permission denied)"
            FAIL_COUNT=$((FAIL_COUNT + 1))
        fi
    else
        echo "   ✗ cpu.max (FAILED - file not found, controller not available)"
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi

    # Test memory.high = 8G
    if [ -f "$TEST_CGROUP_PATH/memory.high" ]; then
        if echo "8G" | sudo tee "$TEST_CGROUP_PATH/memory.high" > /dev/null 2>&1; then
            echo "   ✅ memory.high = 8G (SUCCESS)"
            SUCCESS_COUNT=$((SUCCESS_COUNT + 1))
        else
            echo "   ✗ memory.high = 8G (FAILED - permission denied)"
            FAIL_COUNT=$((FAIL_COUNT + 1))
        fi
    else
        echo "   ✗ memory.high (FAILED - file not found, controller not delegated)"
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi

    # Test memory.max = 8G
    if [ -f "$TEST_CGROUP_PATH/memory.max" ]; then
        if echo "8G" | sudo tee "$TEST_CGROUP_PATH/memory.max" > /dev/null 2>&1; then
            echo "   ✅ memory.max = 8G (SUCCESS)"
            SUCCESS_COUNT=$((SUCCESS_COUNT + 1))
        else
            echo "   ✗ memory.max = 8G (FAILED - permission denied)"
            FAIL_COUNT=$((FAIL_COUNT + 1))
        fi
    else
        echo "   ✗ memory.max (FAILED - file not found, controller not delegated)"
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi

    # Test memory.swap.max = 0
    if [ -f "$TEST_CGROUP_PATH/memory.swap.max" ]; then
        if echo "0" | sudo tee "$TEST_CGROUP_PATH/memory.swap.max" > /dev/null 2>&1; then
            echo "   ✅ memory.swap.max = 0 (SUCCESS)"
            SUCCESS_COUNT=$((SUCCESS_COUNT + 1))
        else
            echo "   ✗ memory.swap.max = 0 (FAILED - permission denied)"
            FAIL_COUNT=$((FAIL_COUNT + 1))
        fi
    else
        echo "   ✗ memory.swap.max (FAILED - file not found, controller not delegated)"
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi

    # Test io.weight = 100
    if [ -f "$TEST_CGROUP_PATH/io.weight" ]; then
        if echo "100" | sudo tee "$TEST_CGROUP_PATH/io.weight" > /dev/null 2>&1; then
            echo "   ✅ io.weight = 100 (SUCCESS)"
            SUCCESS_COUNT=$((SUCCESS_COUNT + 1))
        else
            echo "   ✗ io.weight = 100 (FAILED - permission denied)"
            FAIL_COUNT=$((FAIL_COUNT + 1))
        fi
    else
        echo "   ✗ io.weight (FAILED - file not found, controller not delegated)"
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi

    echo
    echo "   Summary: $SUCCESS_COUNT succeeded, $FAIL_COUNT failed (out of 6 settings)"
    echo

    # Cleanup test cgroup
    sudo rmdir "$TEST_CGROUP_PATH" 2>/dev/null
    echo "   ✓ Cleaned up test cgroup"
else
    echo "   ✗ Failed to create test cgroup at $TEST_CGROUP_PATH"
    echo "   This indicates permission issues or systemd restrictions"
fi

echo
echo "=== Recommendations ==="
echo

if [ "$SYSTEMD_MANAGED" = true ]; then
    if [ "$SUCCESS_COUNT" -eq 6 ]; then
        echo "✅ All controllers working! Your benchmark has full cgroup support."
    elif [ "$SUCCESS_COUNT" -ge 2 ] && [ "$SUCCESS_COUNT" -lt 6 ]; then
        echo "⚠️  Partial controller support detected ($SUCCESS_COUNT/6 working)"
        echo
        echo "Working controllers provide basic fairness (likely CPU controls)."
        echo "For full memory and I/O control, choose one option:"
        echo
        echo "Option 1: Use systemd-run (RECOMMENDED - no system changes needed):"
        echo "  sudo systemd-run --scope --unit=fairness-benchmark \\"
        echo "    -p CPUWeight=100 -p CPUQuota=100% \\"
        echo "    -p MemoryHigh=8G -p MemoryMax=8G -p MemorySwapMax=0 \\"
        echo "    -p IOWeight=100 \\"
        echo "    ./fairness_benchmark dual"
        echo
        echo "Option 2: Enable controller delegation (requires root + system restart):"
        echo "  1. Edit /etc/systemd/system.conf and add:"
        echo "     [Manager]"
        echo "     DefaultCPUAccounting=yes"
        echo "     DefaultMemoryAccounting=yes"
        echo "     DefaultIOAccounting=yes"
        echo "  2. Run: sudo systemctl daemon-reexec"
        echo
        echo "Option 3: Run benchmark as-is:"
        echo "  Current setup ($SUCCESS_COUNT/6 controllers) is sufficient for basic"
        echo "  CPU fairness testing. Memory and I/O will use system defaults."
    else
        echo "✗ No or minimal controller support ($SUCCESS_COUNT/6 working)"
        echo
        echo "To run the benchmark with cgroup controls, use systemd-run:"
        echo "  sudo systemd-run --scope --unit=fairness-benchmark \\"
        echo "    -p CPUWeight=100 -p MemoryHigh=8G -p IOWeight=100 \\"
        echo "    ./fairness_benchmark dual"
    fi
else
    echo "✓ Non-systemd cgroup setup detected"
    if [ "$SUCCESS_COUNT" -ge 4 ]; then
        echo "  Most controllers are working. Run benchmark normally:"
        echo "  sudo ./fairness_benchmark dual"
    else
        echo "  Run with elevated permissions: sudo ./fairness_benchmark dual"
    fi
fi

echo