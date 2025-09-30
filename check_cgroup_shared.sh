#!/bin/bash
# Script to check if cgroup_shared configuration is working correctly

CGROUP_BASE="/sys/fs/cgroup"

# Check if running under systemd
if [ -d "/sys/fs/cgroup/system.slice" ]; then
    CGROUP_BASE="/sys/fs/cgroup/user.slice"
fi

echo "=== Checking Shared Cgroup Configuration ==="
echo ""

# Check parent cgroup
PARENT_PATH="$CGROUP_BASE/clients"
echo "1. Parent cgroup: $PARENT_PATH"
if [ -d "$PARENT_PATH" ]; then
    echo "   ✓ Exists"

    if [ -f "$PARENT_PATH/memory.max" ]; then
        MAX=$(cat "$PARENT_PATH/memory.max" 2>/dev/null)
        echo "   memory.max = $MAX"

        # Convert 2G to bytes for comparison
        if [ "$MAX" = "2147483648" ]; then
            echo "   ✓ Correctly set to 2G"
        elif [ "$MAX" = "max" ]; then
            echo "   ⚠️  Set to 'max' (unlimited)"
        else
            echo "   ⚠️  Unexpected value: $MAX"
        fi
    else
        echo "   ✗ memory.max file not found"
    fi

    if [ -f "$PARENT_PATH/memory.current" ]; then
        CURRENT=$(cat "$PARENT_PATH/memory.current" 2>/dev/null)
        CURRENT_MB=$((CURRENT / 1024 / 1024))
        echo "   memory.current = ${CURRENT_MB}MB"
    fi

    if [ -f "$PARENT_PATH/cgroup.procs" ]; then
        PROCS=$(cat "$PARENT_PATH/cgroup.procs" 2>/dev/null | wc -l)
        echo "   Processes in parent: $PROCS"
    fi
else
    echo "   ✗ Does not exist"
fi

echo ""

# Check client1 cgroup
CLIENT1_PATH="$PARENT_PATH/client1"
echo "2. Client1 cgroup: $CLIENT1_PATH"
if [ -d "$CLIENT1_PATH" ]; then
    echo "   ✓ Exists"

    if [ -f "$CLIENT1_PATH/cgroup.procs" ]; then
        PROCS=$(cat "$CLIENT1_PATH/cgroup.procs" 2>/dev/null | wc -l)
        echo "   Processes in client1: $PROCS"
        if [ $PROCS -gt 0 ]; then
            echo "   PIDs: $(cat "$CLIENT1_PATH/cgroup.procs" 2>/dev/null | tr '\n' ' ')"
        fi
    fi

    if [ -f "$CLIENT1_PATH/memory.current" ]; then
        CURRENT=$(cat "$CLIENT1_PATH/memory.current" 2>/dev/null)
        CURRENT_MB=$((CURRENT / 1024 / 1024))
        echo "   memory.current = ${CURRENT_MB}MB"
    fi
else
    echo "   ✗ Does not exist"
fi

echo ""

# Check client2 cgroup
CLIENT2_PATH="$PARENT_PATH/client2"
echo "3. Client2 cgroup: $CLIENT2_PATH"
if [ -d "$CLIENT2_PATH" ]; then
    echo "   ✓ Exists"

    if [ -f "$CLIENT2_PATH/cgroup.procs" ]; then
        PROCS=$(cat "$CLIENT2_PATH/cgroup.procs" 2>/dev/null | wc -l)
        echo "   Processes in client2: $PROCS"
        if [ $PROCS -gt 0 ]; then
            echo "   PIDs: $(cat "$CLIENT2_PATH/cgroup.procs" 2>/dev/null | tr '\n' ' ')"
        fi
    fi

    if [ -f "$CLIENT2_PATH/memory.current" ]; then
        CURRENT=$(cat "$CLIENT2_PATH/memory.current" 2>/dev/null)
        CURRENT_MB=$((CURRENT / 1024 / 1024))
        echo "   memory.current = ${CURRENT_MB}MB"
    fi
else
    echo "   ✗ Does not exist"
fi

echo ""
echo "=== Summary ==="
if [ -d "$PARENT_PATH" ]; then
    PARENT_MAX=$(cat "$PARENT_PATH/memory.max" 2>/dev/null)
    PARENT_CURRENT=$(cat "$PARENT_PATH/memory.current" 2>/dev/null)
    PARENT_CURRENT_MB=$((PARENT_CURRENT / 1024 / 1024))

    C1_CURRENT=$(cat "$CLIENT1_PATH/memory.current" 2>/dev/null || echo "0")
    C1_CURRENT_MB=$((C1_CURRENT / 1024 / 1024))

    C2_CURRENT=$(cat "$CLIENT2_PATH/memory.current" 2>/dev/null || echo "0")
    C2_CURRENT_MB=$((C2_CURRENT / 1024 / 1024))

    TOTAL_MB=$((C1_CURRENT_MB + C2_CURRENT_MB))

    echo "Parent limit: $PARENT_MAX"
    echo "Parent usage: ${PARENT_CURRENT_MB}MB"
    echo "Client1 usage: ${C1_CURRENT_MB}MB"
    echo "Client2 usage: ${C2_CURRENT_MB}MB"
    echo "Total (C1+C2): ${TOTAL_MB}MB"

    if [ "$PARENT_MAX" = "2147483648" ]; then
        echo "✓ Configuration appears correct (2G limit on parent)"
    else
        echo "⚠️  Parent limit is not 2G"
    fi
else
    echo "⚠️  Parent cgroup not found - shared config may not be working"
fi
