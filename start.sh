#!/bin/bash
# 股票分析系统启动脚本

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

# 检查虚拟环境
if [ ! -f "venv/bin/activate" ]; then
    error "虚拟环境未找到，请先运行 setup.sh"
    exit 1
fi

# 激活虚拟环境
source venv/bin/activate

# 检查Python版本
PYTHON_VERSION=$(python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
if [ $(echo "$PYTHON_VERSION < 3.10" | bc) -eq 1 ]; then
    error "需要Python 3.10+，当前版本: $PYTHON_VERSION"
    exit 1
fi
log "Python版本: $PYTHON_VERSION"

# 检查配置文件
if [ ! -f ".env" ]; then
    warn "未找到 .env 文件，使用 .env.example"
    if [ -f ".env.example" ]; then
        cp .env.example .env
        warn "请编辑 .env 文件配置API密钥"
    else
        error "未找到 .env.example 文件"
        exit 1
    fi
fi

# 创建必要目录
mkdir -p data logs reports

# 解析命令行参数
MODE="analyze"
DEBUG=false
DRY_RUN=false
SERVE_ONLY=false
HOST="0.0.0.0"
PORT="8000"

while [[ $# -gt 0 ]]; do
    case $1 in
        --debug)
            DEBUG=true
            shift
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --serve-only)
            SERVE_ONLY=true
            shift
            ;;
        --host)
            HOST="$2"
            shift 2
            ;;
        --port)
            PORT="$2"
            shift 2
            ;;
        --help)
            echo "用法: $0 [选项]"
            echo ""
            echo "选项:"
            echo "  --debug         调试模式"
            echo "  --dry-run       仅获取数据不分析"
            echo "  --serve-only    仅启动Web服务"
            echo "  --host HOST     Web服务绑定地址 (默认: 0.0.0.0)"
            echo "  --port PORT     Web服务端口 (默认: 8000)"
            echo "  --help          显示帮助信息"
            echo ""
            echo "示例:"
            echo "  $0                     # 运行分析"
            echo "  $0 --debug             # 调试模式运行"
            echo "  $0 --serve-only        # 启动Web服务"
            echo "  $0 --serve-only --port 8080  # 在8080端口启动Web服务"
            exit 0
            ;;
        *)
            error "未知选项: $1"
            echo "使用 --help 查看帮助"
            exit 1
            ;;
    esac
done

# 设置环境变量
export PYTHONPATH="$SCRIPT_DIR:$PYTHONPATH"

if [ "$SERVE_ONLY" = true ]; then
    info "启动Web服务..."
    info "访问地址: http://$HOST:$PORT"
    python main.py --serve-only --host "$HOST" --port "$PORT"
elif [ "$DRY_RUN" = true ]; then
    info "运行干测试（仅获取数据）..."
    python main.py --dry-run
elif [ "$DEBUG" = true ]; then
    info "运行调试模式..."
    python main.py --debug
else
    info "运行股票分析..."
    python main.py
fi