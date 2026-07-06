#!/bin/bash
# Collector Management Script

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_DIR="$SCRIPT_DIR/services"

case "$1" in
    install)
        echo "Installing collector services..."
        sudo cp "$SERVICE_DIR"/*.service /etc/systemd/system/
        sudo systemctl daemon-reload
        echo "Services installed. Use: $0 start"
        ;;
    start)
        echo "Starting collectors..."
        sudo systemctl start polymarket-collector kalshi-collector predictit-collector
        echo "Started. Use: $0 status"
        ;;
    stop)
        echo "Stopping collectors..."
        sudo systemctl stop polymarket-collector kalshi-collector predictit-collector
        ;;
    restart)
        echo "Restarting collectors..."
        sudo systemctl restart polymarket-collector kalshi-collector predictit-collector
        ;;
    status)
        echo "=== Collector Status ==="
        for svc in polymarket-collector kalshi-collector predictit-collector; do
            status=$(systemctl is-active $svc 2>/dev/null || echo "not installed")
            echo "$svc: $status"
        done
        echo ""
        echo "=== Database Status ==="
        cd "$SCRIPT_DIR"
        python3 polymarket_collector.py --status 2>/dev/null || echo "Polymarket: no data"
        python3 kalshi_collector.py --status 2>/dev/null || echo "Kalshi: no data"
        python3 predictit_collector.py --status 2>/dev/null || echo "PredictIt: no data"
        ;;
    logs)
        echo "=== Recent Logs ==="
        journalctl -u polymarket-collector -u kalshi-collector -u predictit-collector --since "1 hour ago" -n 50
        ;;
    test)
        echo "Testing collectors (single snapshot each)..."
        cd "$SCRIPT_DIR"
        echo ""
        echo "--- PredictIt ---"
        timeout 30 python3 -c "
from predictit_collector import init_database, collect_snapshot
conn = init_database()
m, c = collect_snapshot(conn)
print(f'Collected {m} markets, {c} contracts')
conn.close()
"
        echo ""
        echo "--- Polymarket ---"
        timeout 60 python3 -c "
from polymarket_collector import init_database, discover_markets, collect_snapshot
conn = init_database()
markets = discover_markets(10)
for m in markets[:3]:
    collect_snapshot(conn, m)
    print(f'Collected: {m[\"question\"][:50]}')
conn.close()
"
        echo ""
        echo "--- Kalshi ---"
        timeout 60 python3 -c "
from kalshi_collector import init_database, load_credentials, discover_markets, collect_snapshot
key_id, pk = load_credentials()
conn = init_database()
markets = discover_markets(key_id, pk, 10)
for m in markets[:3]:
    collect_snapshot(conn, key_id, pk, m)
    print(f'Collected: {m[\"title\"][:50]}')
conn.close()
"
        ;;
    *)
        echo "Usage: $0 {install|start|stop|restart|status|logs|test}"
        exit 1
        ;;
esac
