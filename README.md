# XiaoGPT Router

面向 OpenWrt / iStoreOS Docker 路由器的 XiaoGPT 部署版本，增加免费的 Edge 中文语音连续播报。

## 一键安装

**在路由器的 SSH 终端粘贴以下整段命令：下载完成后自动运行安装向导。**

```sh
XIAOGPT_INSTALLER="$(mktemp)" && \
curl -fL --retry 3 https://raw.githubusercontent.com/dongbo501/xiaogpt-router/main/install.sh -o "$XIAOGPT_INSTALLER" && \
sh "$XIAOGPT_INSTALLER" && rm -f "$XIAOGPT_INSTALLER"
```

需要 Linux、已安装并运行的 Docker，以及 `curl`；不需要 Git、Python 或 Docker Compose。镜像支持 x86_64 / ARM64，目前仅在 x86_64 路由器上完成音箱实测。

<details>
<summary>路由器只有 wget？使用这条命令</summary>

```sh
XIAOGPT_INSTALLER="$(mktemp)" && \
wget -O "$XIAOGPT_INSTALLER" https://raw.githubusercontent.com/dongbo501/xiaogpt-router/main/install.sh && \
sh "$XIAOGPT_INSTALLER" && rm -f "$XIAOGPT_INSTALLER"
```

</details>

向导会让你选择**持久安装目录**（建议外接硬盘），下载源码、构建镜像，然后引导填写路由器地址、小米账号、API 地址、Key 和模型名，完成验证码登录并选择音箱。密码和 Key 不回显；最后自动后台启动并检查服务就绪。默认音色为 Edge 晓晓，触发词包括“请问”和“补充”。

检测到已有 `xiaogpt` 容器时会停止安装，不替换旧服务。构建或配置失败后可重新运行命令、选择原目录继续；已有配置不会被覆盖。若容器已创建但启动失败，请先查看 `docker logs --tail 100 xiaogpt`。

一键安装的配置位于 `<安装目录>/config/config.json`，音频服务地址位于 `<安装目录>/config/router.env`。修改普通配置后执行 `docker restart xiaogpt`。`router.env` 在创建容器时加载，修改网络地址需要重新创建容器。常用管理命令：

```sh
docker logs -f xiaogpt       # 查看日志
docker restart xiaogpt       # 重启服务
docker stop xiaogpt          # 停止服务
```

安装脚本：[install.sh](install.sh) · 配置向导：[setup.py](deploy/router/setup.py)。安装过程需要访问 GitHub、Docker Hub 和 PyPI，小米登录及 Edge 语音也需要联网。

基于 [yihong0618/xiaogpt](https://github.com/yihong0618/xiaogpt)，源码基线为 [`4258b0b53cf6af062f1a01af993d47a161382a0f`](https://github.com/yihong0618/xiaogpt/commit/4258b0b53cf6af062f1a01af993d47a161382a0f)。保留上游 MIT 许可证；原始说明见 [README.upstream.md](README.upstream.md)。

## 本版本的改动

- 固定 `edge-tts==7.3.1`，修复旧版请求校验导致的 HTTP 403。
- Edge 音频边生成边传给音箱，一次回答只启动一个播放 URL，提前合成后续文字。
- 音频缓存支持多个请求独立读取及 Range 重试，并处理播放结束后的重复请求。
- 语音合成前过滤网址、常见域名和链接引用，保留可读的网站名称；支持链接跨流式片段返回，按句处理，无需等待整段回答结束。
- 开启 `mute_xiaoai` 时，发现有效提问就直接暂停小爱，不再先查询播放状态；保留“正在问……”提示和原有约一秒的轮询等待。
- 将触发词连同提问完整传给模型，保留“请对比”“请详细向我解释”等词表达的任务意图。
- 保留小爱原声选项，对较长原声播报分段并检查指令返回结果。
- 修复 Python 3.14 启动入口，以及小米短信验证后的确认跳转。

已在 x86_64 iStoreOS、Intel J4125 / 4 GB 内存、小爱音箱 Play 增强版 L05C 上验证。实际固件支持外部 MP3 和渐进式 HTTP 音频播放；其他型号仍需验证。

默认使用“晓晓”中文女声、流式回答、Edge 语音音量 `+40%`。本版本不修改音箱自身音量。Edge 需要联网；聊天模型仍使用用户自己的 API，费用取决于聊天服务商。

## 手动 Docker Compose 部署

请使用 `deploy/router/Dockerfile`。根目录 Dockerfile 和 PDM 锁文件保留上游版本，不包含本部署层的 Edge 升级。

```sh
git clone https://github.com/dongbo501/xiaogpt-router.git
cd xiaogpt-router
mkdir -p config
cp deploy/router/config.example.yaml config/config.yaml
cp deploy/router/router.env.example deploy/router/router.env
chmod 600 config/config.yaml deploy/router/router.env
```

编辑 `config/config.yaml`，填写小米账号、密码、设备 DID、聊天 API 地址、Key 和模型名。将 `deploy/router/router.env` 中的 `XIAOGPT_HOSTNAME` 改为音箱能访问的路由器局域网地址。

首次登录如需短信验证，可以先在终端前台启动，按提示完成验证；令牌保存在 `config/` 中。完成后用 Ctrl+C 退出，再后台启动：

```sh
docker compose --env-file deploy/router/router.env -f deploy/router/compose.yaml build
docker compose --env-file deploy/router/router.env -f deploy/router/compose.yaml run --rm -it xiaogpt
docker compose --env-file deploy/router/router.env -f deploy/router/compose.yaml up -d
docker compose --env-file deploy/router/router.env -f deploy/router/compose.yaml logs -f
```

已有 `xiaogpt` 容器时，先备份配置并停止旧容器，再迁移，避免两个服务同时处理音箱。Compose 使用 Linux host 网络，音频只监听配置的局域网地址，默认端口 9527。

## 使用

“小爱同学，请问太阳为什么会发光？”

回答 AI 的追问时，可以说：“小爱同学，补充，两个人，不吃辣。”

示例配置还提供“请向我解释”“请详细向我解释”触发词，以及“开始持续对话”“结束持续对话”。

## 离线检查

```sh
docker run --rm --network none \
  -v "$PWD:/repo:ro" -w /repo \
  --entrypoint /app/.venv/bin/python xiaogpt-router:local \
  -m unittest discover -s tests -v
```

GitHub Actions 仅提供手动检查，不自动发布 Docker Hub 镜像或 PyPI 包。

## 配置与来源

仓库只提供空凭据模板。实际配置、`.mi.token`、Cookie、环境文件、日志和音频不应提交到 Git。

`deploy/router/miaccount.py` 基于 [Yonsm/MiService](https://github.com/Yonsm/MiService) 3.0.1，许可证见 [MISERVICE-LICENSE](deploy/router/MISERVICE-LICENSE)。Edge 客户端通过 PyPI 安装，来源为 [rany2/edge-tts](https://github.com/rany2/edge-tts)。微软调整在线朗读协议时，可能需要更新客户端。
