#!/bin/sh
# First installation only. Existing containers and user configuration are kept.
set -eu
umask 077

SOURCE_URL=https://codeload.github.com/dongbo501/xiaogpt-router/tar.gz/refs/heads/main
IMAGE=${XIAOGPT_IMAGE:-ghcr.io/dongbo501/xiaogpt-router:latest}
BUILD_IMAGE=0
INSTALL_DIR=
LOCK_DIR=
ARCHIVE=

fail() { printf '\n安装未完成：%s\n' "$*" >&2; exit 1; }
cleanup() {
    if [ -n "$ARCHIVE" ]; then rm -f "$ARCHIVE"; fi
    if [ -n "$LOCK_DIR" ]; then rmdir "$LOCK_DIR" 2>/dev/null || :; fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

while [ "$#" -gt 0 ]; do
    case "$1" in
        --dir)
            [ "$#" -ge 2 ] || fail '--dir 后需要指定安装目录'
            INSTALL_DIR=$2
            shift 2
            ;;
        --build)
            BUILD_IMAGE=1
            IMAGE=${XIAOGPT_IMAGE:-xiaogpt-router:local}
            shift
            ;;
        -h|--help)
            printf '用法：sh install.sh [--dir /持久磁盘/xiaogpt-router] [--build]\n默认拉取发布镜像；--build 从源码本地构建。\n需要 Linux、已运行的 Docker、curl 或 wget，以及交互终端。\n'
            exit 0
            ;;
        *) fail "未知参数：$1" ;;
    esac
done

[ "$(uname -s)" = Linux ] || fail '请在 Linux 路由器的 SSH 终端运行'
case "$(uname -m)" in
    x86_64|aarch64|arm64) ;;
    *) fail '镜像仅支持 x86_64 / ARM64；不支持 MIPS 或 32 位 ARM' ;;
esac
command -v docker >/dev/null 2>&1 || fail '请先在路由器上安装并启动 Docker'
docker info >/dev/null 2>&1 || fail '无法连接 Docker，请检查服务和当前用户权限'
if docker container inspect xiaogpt >/dev/null 2>&1; then
    fail '已有名为 xiaogpt 的容器（包括停止状态）。本脚本不会替换它；请保留现有服务，按 README 的手动迁移说明处理。'
fi
command -v tar >/dev/null 2>&1 || fail '缺少 tar 命令'
if command -v curl >/dev/null 2>&1; then
    DOWNLOADER=curl
elif command -v wget >/dev/null 2>&1; then
    DOWNLOADER=wget
else
    fail '请先安装 curl 或 wget'
fi
[ -t 0 ] && [ -t 1 ] || fail '需要交互终端填写配置。请先下载脚本，再用 sh install.sh 运行，不要通过管道传给 sh。'

printf '\nXiaoGPT Router 安装向导\n建议将源码和配置放在外接硬盘或其他持久目录，不要使用 /tmp。\n'
if [ -z "$INSTALL_DIR" ]; then
    printf '安装目录 [/opt/xiaogpt-router]：'
    IFS= read -r INSTALL_DIR || exit 1
    INSTALL_DIR=${INSTALL_DIR:-/opt/xiaogpt-router}
fi
case "$INSTALL_DIR" in
    /*) ;;
    *) fail '请使用绝对路径，例如 /mnt/sda1/xiaogpt-router' ;;
esac
case "$INSTALL_DIR" in
    *:*|*'
'*) fail '安装目录不能包含冒号或换行' ;;
esac

if [ -e "$INSTALL_DIR" ]; then
    [ -f "$INSTALL_DIR/.xiaogpt-router-install" ] || fail '目录已存在且不是本安装器创建的目录；请选择新的目录'
    [ "$(cat "$INSTALL_DIR/.xiaogpt-router-install")" = xiaogpt-router-installer-v1 ] || fail '安装标记不匹配'
else
    mkdir -p "$INSTALL_DIR"
    printf '%s\n' xiaogpt-router-installer-v1 > "$INSTALL_DIR/.xiaogpt-router-install"
fi
INSTALL_DIR=$(cd "$INSTALL_DIR" && pwd -P)
mkdir "$INSTALL_DIR/.install-lock" 2>/dev/null || fail '安装目录正被另一个安装进程使用；若上次被强制终止，请确认没有安装进程后移除 .install-lock'
LOCK_DIR=$INSTALL_DIR/.install-lock

if [ ! -f "$INSTALL_DIR/.source-ready" ]; then
    printf '\n下载项目源码……\n'
    ARCHIVE=$INSTALL_DIR/.source.tar.gz
    if [ "$DOWNLOADER" = curl ]; then
        curl -fL --retry 3 --connect-timeout 20 "$SOURCE_URL" -o "$ARCHIVE"
    else
        wget -O "$ARCHIVE" "$SOURCE_URL"
    fi
    tar -xzf "$ARCHIVE" --strip-components=1 -C "$INSTALL_DIR"
    rm -f "$ARCHIVE"
    ARCHIVE=
    [ -f "$INSTALL_DIR/deploy/router/Dockerfile" ] &&
        [ -f "$INSTALL_DIR/deploy/router/setup.py" ] || fail '下载的源码不完整'
    touch "$INSTALL_DIR/.source-ready"
else
    printf '\n继续上次未完成的安装，复用已下载的源码。\n'
fi

mkdir -p "$INSTALL_DIR/config"
chmod 700 "$INSTALL_DIR/config"
if [ "$BUILD_IMAGE" = 1 ]; then
    printf '\n从源码构建 Docker 镜像……\n'
    docker build -f "$INSTALL_DIR/deploy/router/Dockerfile" -t "$IMAGE" "$INSTALL_DIR"
else
    printf '\n拉取发布镜像 %s……\n' "$IMAGE"
    docker pull "$IMAGE" || fail '拉取镜像失败；请检查 GHCR 网络访问，或使用 --build 从源码构建'
fi

printf '\n填写配置并登录小米账号（密码和 API Key 不回显）……\n'
docker run --rm -it --network host \
    -e TZ=Asia/Shanghai \
    -e XIAOGPT_MI_TOKEN_PATH=/config/.mi.token \
    -v "$INSTALL_DIR/config:/config" \
    -v "$INSTALL_DIR/deploy/router:/setup:ro" \
    --entrypoint /app/.venv/bin/python "$IMAGE" /setup/setup.py

[ -s "$INSTALL_DIR/config/config.json" ] &&
    [ -s "$INSTALL_DIR/config/router.env" ] || fail '配置向导未完成，没有启动服务'
printf '\n启动 XiaoGPT……\n'
docker run -d --name xiaogpt --restart unless-stopped \
    --network host \
    --label com.centurylinklabs.watchtower.enable=false \
    --log-driver json-file --log-opt max-size=5m --log-opt max-file=3 \
    -e TZ=Asia/Shanghai -e PYTHONUNBUFFERED=1 \
    --env-file "$INSTALL_DIR/config/router.env" \
    -v "$INSTALL_DIR/config:/config" \
    "$IMAGE" --config=/config/config.json

attempt=0
while [ "$attempt" -lt 60 ]; do
    state=$(docker inspect --format '{{.State.Status}} {{.RestartCount}}' xiaogpt)
    [ "$state" = 'running 0' ] || fail '容器未正常运行。请用 docker logs --tail 100 xiaogpt 查看原因，配置与容器已保留。'
    if docker logs xiaogpt 2>&1 | grep -q 'Running xiaogpt now'; then
        printf '\n安装完成，服务已进入监听状态。\n配置：%s/config/config.json\n网络设置：%s/config/router.env\n\n试着说：小爱同学，请问太阳为什么会发光？\n查看日志：docker logs -f xiaogpt\n重启服务：docker restart xiaogpt\n' "$INSTALL_DIR" "$INSTALL_DIR"
        printf '\nWeb 管理（路由器本机）：http://127.0.0.1:9988\n查看首次管理密码：docker exec xiaogpt cat /config/web-initial-password.txt\n在电脑上使用 SSH 转发后即可通过浏览器修改设置。详见 README 的 Web 管理章节。\n'
        exit 0
    fi
    attempt=$((attempt + 1))
    sleep 2
done
fail '等待服务就绪超时。请用 docker logs --tail 100 xiaogpt 检查；已有配置和容器均已保留。'
