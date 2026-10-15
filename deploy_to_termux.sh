#!/usr/bin/env bash
# ==============================================================================
# QQ智能小秘书 - 安卓 Termux 一键部署与同步脚本
# ==============================================================================

set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TERMUX_DEST_DIR="~/qq-secretary"

echo "=============================================================="
echo "📱 QQ智能小秘书 - Termux 部署助手"
echo "=============================================================="
echo "请选择部署方式："
echo "  1) 通过 ADB (USB 或无线调试已连接)"
echo "  2) 通过 SCP / SSH (Termux 中运行 sshd，端口默认 8022)"
echo "  3) 仅在手机本地 Termux 中直接初始化并运行"
echo "=============================================================="
read -p "请输入选项 [1/2/3]: " choice

case "$choice" in
  1)
    echo "🔍 检查 ADB 连接状态..."
    if ! command -v adb &> /dev/null; then
        echo "❌ 错误: 本地未安装 adb 命令行工具！"
        exit 1
    fi
    adb devices
    echo "📦 正在推送代码至手机存储目录 (/sdcard/qq-secretary)..."
    adb shell "mkdir -p /sdcard/qq-secretary"
    adb push "$PROJECT_DIR/config.example.json" /sdcard/qq-secretary/
    if [ -f "$PROJECT_DIR/config.json" ]; then
        adb push "$PROJECT_DIR/config.json" /sdcard/qq-secretary/
    fi
    adb push "$PROJECT_DIR/requirements.txt" /sdcard/qq-secretary/
    adb push "$PROJECT_DIR/db.py" /sdcard/qq-secretary/
    adb push "$PROJECT_DIR/analyzer.py" /sdcard/qq-secretary/
    adb push "$PROJECT_DIR/notifier.py" /sdcard/qq-secretary/
    adb push "$PROJECT_DIR/main.py" /sdcard/qq-secretary/
    adb push "$PROJECT_DIR/test_suite.py" /sdcard/qq-secretary/
    echo "✅ 文件已推送至 /sdcard/qq-secretary"
    echo ""
    echo "👉 请在手机 Termux 中执行以下命令导入并常驻运行："
    echo "--------------------------------------------------------"
    echo "termux-setup-storage"
    echo "mkdir -p ~/qq-secretary"
    echo "cp -r /sdcard/qq-secretary/* ~/qq-secretary/"
    echo "cd ~/qq-secretary"
    echo "pkg update && pkg install python -y"
    echo "pip install -r requirements.txt"
    echo "termux-wake-lock"
    echo "nohup python main.py > qq_secretary.log 2>&1 &"
    echo "--------------------------------------------------------"
    ;;

  2)
    read -p "请输入手机局域网 IP (例如 192.168.1.100): " phone_ip
    read -p "请输入 Termux SSH 端口 (默认 8022): " phone_port
    phone_port=${phone_port:-8022}

    echo "📦 正在通过 SCP 同步文件到 Termux ($phone_ip:$phone_port)..."
    ssh -p "$phone_port" "$phone_ip" "mkdir -p $TERMUX_DEST_DIR"
    scp -P "$phone_port" \
        "$PROJECT_DIR/requirements.txt" \
        "$PROJECT_DIR/config.example.json" \
        "$PROJECT_DIR/db.py" \
        "$PROJECT_DIR/analyzer.py" \
        "$PROJECT_DIR/notifier.py" \
        "$PROJECT_DIR/main.py" \
        "$PROJECT_DIR/test_suite.py" \
        "$phone_ip:$TERMUX_DEST_DIR/"

    if [ -f "$PROJECT_DIR/config.json" ]; then
        scp -P "$phone_port" "$PROJECT_DIR/config.json" "$phone_ip:$TERMUX_DEST_DIR/"
    fi

    echo "✅ 传输完成！正在远程启动并加持唤醒锁..."
    ssh -p "$phone_port" "$phone_ip" "cd $TERMUX_DEST_DIR && pkg install python -y && pip install -r requirements.txt && termux-wake-lock && nohup python main.py > qq_secretary.log 2>&1 &"
    echo "🎉 远程启动完成！可通过 curl http://$phone_ip:8000/health 检查状态。"
    ;;

  3)
    echo "⚙️ 正在当前 Termux 环境中初始化..."
    if command -v termux-wake-lock &> /dev/null; then
        echo "🔒 获取 Termux 唤醒锁 (防后台睡眠)..."
        termux-wake-lock
    fi

    echo "📦 检查并安装依赖..."
    pip install -r requirements.txt

    echo "🚀 启动后台服务 (nohup)..."
    nohup python main.py > qq_secretary.log 2>&1 &
    sleep 2

    if pgrep -f "python main.py" > /dev/null; then
        echo "🎉 QQ智能小秘书启动成功！进程 PID: $(pgrep -f 'python main.py')"
        echo "📜 查看实时日志命令: tail -f qq_secretary.log"
        echo "🛑 停止服务命令: kill \$(pgrep -f 'python main.py')"
    else
        echo "❌ 启动异常，请查看日志: cat qq_secretary.log"
    fi
    ;;

  *)
    echo "❌ 无效选项，退出。"
    exit 1
    ;;
esac
