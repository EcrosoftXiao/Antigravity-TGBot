#!/usr/bin/env bash
# ==============================================================================
# Antigravity Telegram Bot Launcher & Service Manager
# ==============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PID_FILE="$SCRIPT_DIR/bot.pid"
LOG_FILE="$SCRIPT_DIR/bot.log"
PYTHON_BIN="$(which python3 || echo "")"

if [ -z "$PYTHON_BIN" ]; then
    echo "❌ 错误：在 PATH 中未找到 python3。" >&2
    exit 1
fi

check_env() {
    # 1. Check .env
    if [ ! -f "$SCRIPT_DIR/.env" ]; then
        if [ -f "$SCRIPT_DIR/.env.example" ]; then
            echo "⚠️  未检测到 .env 配置文件，正在根据 .env.example 创建..."
            cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
            echo "⚠️  请编辑 $SCRIPT_DIR/.env 并配置你的 TELEGRAM_BOT_TOKEN。"
        fi
    fi

    # 2. Check dependencies
    if ! "$PYTHON_BIN" -c "import telegram, dotenv" >/dev/null 2>&1; then
        echo "📦 正在从 requirements.txt 安装依赖包..."
        "$PYTHON_BIN" -m pip install -r "$SCRIPT_DIR/requirements.txt"
    fi

    # 3. Check agentapi
    local agentapi_path="$HOME/.gemini/antigravity/bin/agentapi"
    local app_path="/Applications/Antigravity.app/Contents/Resources/bin/language_server"
    if [ ! -f "$agentapi_path" ] && [ ! -f "$app_path" ]; then
        echo "⚠️  警告：在默认路径未检测到原生 Antigravity 运行环境或 agentapi 二进制文件。"
        echo "   请确保已安装 Antigravity。"
    fi

    # 4. Check running language_server
    if ! ps -A -o command | grep -v grep | grep -q "language_server.*--csrf_token"; then
        echo "⚠️  提示：未检测到正在运行的 Antigravity 宿主进程。"
        echo "   请确保 Antigravity IDE 已启动并处于打开状态。"
    fi
}

start_daemon() {
    check_env
    if [ -f "$PID_FILE" ]; then
        local pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            echo "⚠️  Antigravity Telegram Bot 已经在运行中 (PID: $pid)。"
            return 0
        else
            rm -f "$PID_FILE"
        fi
    fi

    echo "🚀 正在后台启动 Antigravity Telegram Bot 服务..."
    nohup "$PYTHON_BIN" "$SCRIPT_DIR/bot.py" >> "$LOG_FILE" 2>&1 &
    local new_pid=$!
    echo "$new_pid" > "$PID_FILE"
    sleep 1

    if kill -0 "$new_pid" 2>/dev/null; then
        echo "✅ Antigravity Telegram Bot 启动成功 (PID: $new_pid)。"
        echo "📄 日志路径: $LOG_FILE (使用 './run.sh logs' 查看实时日志)"
    else
        echo "❌ 启动失败。详情请查看日志: $LOG_FILE"
        exit 1
    fi
}

stop_daemon() {
    if [ ! -f "$PID_FILE" ]; then
        echo "ℹ️  Antigravity Telegram Bot 未运行 (未找到 PID 文件)。"
        return 0
    fi

    local pid=$(cat "$PID_FILE")
    if kill -0 "$pid" 2>/dev/null; then
        echo "🛑 正在停止 Antigravity Telegram Bot (PID: $pid)..."
        kill "$pid" 2>/dev/null || true
        for i in {1..10}; do
            if kill -0 "$pid" 2>/dev/null; then
                sleep 0.5
            else
                break
            fi
        done
        if kill -0 "$pid" 2>/dev/null; then
            echo "⚠️  进程未正常退出，正在强制终止 (kill -9)..."
            kill -9 "$pid" 2>/dev/null || true
        fi
        echo "✅ 机器人服务已停止。"
    else
        echo "ℹ️  进程 $pid 未处于活动状态。"
    fi
    rm -f "$PID_FILE"
}

check_status() {
    if [ -f "$PID_FILE" ]; then
        local pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            echo "🟢 Antigravity Telegram Bot 正在运行中 (PID: $pid)。"
            return 0
        else
            echo "🔴 Antigravity Telegram Bot 已停止 (清理过期 PID)。"
            rm -f "$PID_FILE"
            return 1
        fi
    else
        echo "⚪ Antigravity Telegram Bot 未运行。"
        return 1
    fi
}

view_logs() {
    if [ -f "$LOG_FILE" ]; then
        echo "📄 正在追踪日志 $LOG_FILE (按 Ctrl+C 退出)..."
        tail -f -n 50 "$LOG_FILE"
    else
        echo "ℹ️  暂未发现日志文件 ($LOG_FILE)。"
    fi
}

run_foreground() {
    check_env
    echo "🛸 正在前台启动 Antigravity Telegram Bot..."
    exec "$PYTHON_BIN" "$SCRIPT_DIR/bot.py" "$@"
}

case "${1:-run}" in
    run)
        shift || true
        run_foreground "$@"
        ;;
    start)
        start_daemon
        ;;
    stop)
        stop_daemon
        ;;
    restart)
        stop_daemon
        sleep 1
        start_daemon
        ;;
    status)
        check_status
        ;;
    logs)
        view_logs
        ;;
    help|--help|-h)
        echo "用法: $0 [run|start|stop|restart|status|logs|help]"
        echo ""
        echo "管理指令:"
        echo "  run (默认)   : 在前台运行机器人"
        echo "  start        : 在后台启动守护进程"
        echo "  stop         : 停止正在运行的后台服务"
        echo "  restart      : 重启后台服务"
        echo "  status       : 查看后台服务运行状态"
        echo "  logs         : 实时查看运行日志"
        echo "  help         : 显示此帮助信息"
        ;;
    *)
        echo "未知指令: $1"
        echo "运行 '$0 help' 查看使用说明。"
        exit 1
        ;;
esac
