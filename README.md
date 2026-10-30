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

向导会让你选择**持久安装目录**（建议外接硬盘），下载配置向导、拉取 `ghcr.io/dongbo501/xiaogpt-router:latest` 镜像，然后引导填写路由器地址、小米账号、API 地址、Key 和模型名，完成验证码登录并选择音箱。密码和 Key 不回显；最后自动后台启动并检查服务就绪。默认音色为 Edge 晓晓，触发词包括“请问”和“补充”。需要本地源码构建时，可下载脚本后运行 `sh install.sh --build`。

检测到已有 `xiaogpt` 容器时会停止安装，不替换旧服务。构建或配置失败后可重新运行命令、选择原目录继续；已有配置不会被覆盖。若容器已创建但启动失败，请先查看 `docker logs --tail 100 xiaogpt`。

一键安装的配置位于 `<安装目录>/config/config.json`，音频服务地址位于 `<安装目录>/config/router.env`。修改普通配置后执行 `docker restart xiaogpt`。`router.env` 在创建容器时加载，修改网络地址需要重新创建容器。常用管理命令：

```sh
docker logs -f xiaogpt       # 查看日志
docker restart xiaogpt       # 重启服务
docker stop xiaogpt          # 停止服务
```

安装脚本：[install.sh](install.sh) · 配置向导：[setup.py](deploy/router/setup.py)。默认安装需要访问 GitHub 和 GHCR；源码构建另外需要 Docker Hub 和 PyPI。小米登录及回答的 Edge 语音合成需要联网。

基于 [yihong0618/xiaogpt](https://github.com/yihong0618/xiaogpt)，源码基线为 [`4258b0b53cf6af062f1a01af993d47a161382a0f`](https://github.com/yihong0618/xiaogpt/commit/4258b0b53cf6af062f1a01af993d47a161382a0f)。保留上游 MIT 许可证；原始说明见 [README.upstream.md](README.upstream.md)。

## 本版本的改动

### 中文 Web 管理中心（v1.1.1）

默认地址为**路由器本机** `http://127.0.0.1:9988`。现有部署使用 host 网络，因此直接绑定宿主机回环地址，不需要也不使用 `-p 127.0.0.1:9988:9988`。音箱音频服务继续使用局域网地址和原端口，两者互不影响。管理页默认不监听局域网或公网。

在电脑上通过 SSH 转发访问：

```sh
ssh -N -L 9988:127.0.0.1:9988 root@你的路由器IP
# 然后在这台电脑浏览器打开 http://127.0.0.1:9988
```

也可以将现有反向代理的上游指向路由器 `127.0.0.1:9988`。首次管理密码自动生成，查看方式：

```sh
docker exec xiaogpt cat /config/web-initial-password.txt
```

登录后可在“维护与安全”修改管理密码，初始密码文件随后删除。管理密码与小米密码、API Key 相互独立，登录状态可跨容器重启保留。

管理页面提供：

- **接口与模型**：按“选接口 → 选模型 → 测试并应用”操作。默认保留当前接口和密钥，只换模型无需重填；添加接口时才填写地址与对应密钥。模型从下拉列表选择，也可手动填写。点击“测试并应用到小爱”后，测试通过才会保存并重启；失败不会改变配置。接口收藏管理与高级参数默认折叠。
- **提示词与播报**：回答风格模板、可编辑提示词、等待/限流/额度不足/其他失败的自定义固定提示；留空关闭该提示。Edge 固定语音继续使用持久缓存。
- **音箱与声音**：音色列表与自定义音色 ID、语速/音量/音调选择、触发词、持续对话口令、读取已登录账号下的设备列表。
- **运行与维护**：状态、脱敏日志、配置自动备份与恢复、修改管理密码、保存设置、一键保存并重启容器。

保存前校验配置；检测到其他页面或终端修改时拒绝覆盖。最近 20 份自动备份在 `/config/web-backups/`；接口方案、管理密码和音频缓存均保存在 `/config`，升级镜像时继续保留。

“重启容器”由容器主进程收到请求后退出、Docker 的 `unless-stopped` 策略重新启动来实现，**不挂载 Docker socket**。因此部署须保持 `restart: unless-stopped` 或等价策略。语音服务由独立子进程运行，即使语音登录失败，管理页也会保持可用并显示状态。

可通过 `XIAOGPT_WEB_HOST`、`XIAOGPT_WEB_PORT` 修改监听位置（需重建容器以改变环境变量）；默认 `127.0.0.1:9988`。需要原先的纯命令行入口或交互式小米验证码登录时，设置 `XIAOGPT_WEB_DISABLED=1`。

- 固定 `edge-tts==7.3.1`，修复旧版请求校验导致的 HTTP 403。
- Edge 音频边生成边传给音箱，一次回答只启动一个播放 URL，提前合成后续文字。
- 音频缓存支持多个请求独立读取及 Range 重试，并处理播放结束后的重复请求。
- 语音合成前过滤网址、常见域名和链接引用，保留可读的网站名称；支持链接跨流式片段返回，按句处理，无需等待整段回答结束。
- 开启 `mute_xiaoai` 时，发现有效提问就直接暂停小爱，不再先查询播放状态；保留原有约一秒的轮询等待。
- Edge 模式直接播放固定提示“正在问 GPT，请耐心等待”，MP3 持久化保存在 `/config/audio-cache`，同样的文本和音色不会重复合成，也不调用小米文字播报接口。
- 等待提示播放与 GPT 请求并发进行，提前返回的文字先缓冲，提示完整播放后才播回答，避免声音重叠。提示失败或超时只记录警告，继续问答，不再退出整个服务。
- 将触发词连同提问完整传给模型，保留“请对比”“请详细向我解释”等词表达的任务意图。
- 普通模式每次只发送当前提问和配置提示词，减少历史消息的输入 token；持续对话模式保留当前会话的多轮上下文。
- Edge 播报期间继续按约一秒的间隔查询新问题；收到有效提问时，结束旧音频和生成任务，再播放新回答，支持中途打断。
- 保留小爱原声选项，对较长原声播报分段并检查指令返回结果。
- 修复 Python 3.14 启动入口，以及小米短信验证后的确认跳转。

已在 x86_64 iStoreOS、Intel J4125 / 4 GB 内存、小爱音箱 Play 增强版 L05C 上验证。实际固件支持外部 MP3 和渐进式 HTTP 音频播放；其他型号仍需验证。

默认使用“晓晓”中文女声、流式回答、Edge 语音音量 `+40%`。本版本不修改音箱自身音量。Edge 需要联网；聊天模型仍使用用户自己的 API，费用取决于聊天服务商。

## 手动 Docker Compose 部署

默认使用已发布的 GHCR 镜像。需要自行构建时请使用 `deploy/router/Dockerfile`；根目录 Dockerfile 和 PDM 锁文件保留上游版本，不包含本部署层的 Edge 升级。

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
docker compose --env-file deploy/router/router.env -f deploy/router/compose.yaml pull
docker compose --env-file deploy/router/router.env -f deploy/router/compose.yaml run --rm -it -e XIAOGPT_WEB_DISABLED=1 xiaogpt
docker compose --env-file deploy/router/router.env -f deploy/router/compose.yaml up -d
docker compose --env-file deploy/router/router.env -f deploy/router/compose.yaml logs -f
```

已有 `xiaogpt` 容器时，先备份配置并停止旧容器，再迁移，避免两个服务同时处理音箱。Compose 使用 Linux host 网络，音频只监听配置的局域网地址，默认端口 9527。

## 镜像更新与 DPanel

稳定更新标签为 `ghcr.io/dongbo501/xiaogpt-router:latest`，同时发布 `v1.0.0` 等版本标签和提交标签。镜像包含完整项目代码及兼容补丁；部署只需要持久化 `/config`，不要再将旧的 `xiaogpt.py`、`edge.py`、`miaccount.py` 等文件覆盖挂载到容器中。

要让 DPanel 检查稳定版更新，创建容器时应明确填写完整的 `:latest` 镜像引用。已经用 `@sha256:...` 或镜像 ID 创建的容器需要重新创建；只对本地镜像执行 `docker tag` 不会修改容器的 `Config.Image`。保留原 `/config` 挂载后，账号令牌、聊天配置和固定提示音缓存均可继续使用。

Compose 更新命令：

```sh
docker compose --env-file deploy/router/router.env -f deploy/router/compose.yaml pull
docker compose --env-file deploy/router/router.env -f deploy/router/compose.yaml up -d
```

镜像版本是否更新由 DPanel 手动检查，默认仍禁用 Watchtower 自动替换本服务。源码构建产生的 `xiaogpt-router:local` 不提供注册表更新检测。

## 使用

“小爱同学，请问太阳为什么会发光？”

普通模式每次都是独立提问，只发送本次问题和配置中的提示词，不发送之前的问答。“补充”仍可触发回答，但不会自动关联上一问。

需要多轮交流时，先说“小爱同学，开始持续对话”，再开始提问。此模式下后续问题不必带触发词，会携带本次会话的历史；说“结束持续对话”后清空会话，下次开启时重新开始。

例如，在持续对话中询问餐厅后，可以接着说：“补充，两个人，不吃辣。”持续对话仍沿用原有历史保留策略：ChatGPT 接口保留首轮（包含提示词）和最近五轮，实际发送的历史最多六轮。

示例配置还提供“请向我解释”“请详细向我解释”触发词，以及“开始持续对话”“结束持续对话”。

回答尚未播完时，可以重新唤醒“小爱同学”并提出新问题。普通模式的新问题仍需带触发词，例如“请问月亮为什么绕着地球转”；持续对话模式无需触发词。程序收到小米云端的新提问记录后，会结束旧回答并接管新问题，不必等旧音频播完。接管速度仍受小米云端记录更新和网络延迟影响。

## 离线检查

```sh
docker build -f deploy/router/Dockerfile -t xiaogpt-router:local .
docker run --rm --network none \
  -v "$PWD:/repo:ro" -w /repo \
  --entrypoint /app/.venv/bin/python xiaogpt-router:local \
  -m unittest discover -s tests -v
```

GitHub Actions 保留手动离线检查；推送 `v*` 标签会先运行测试，再发布 AMD64 / ARM64 镜像到 GHCR，并创建 GitHub 源码 Release。发布流程也支持手动运行。不发布 Docker Hub 镜像或 PyPI 包。

## 配置与来源

仓库只提供空凭据模板。实际配置、`.mi.token`、Cookie、环境文件、日志和音频不应提交到 Git。

`deploy/router/miaccount.py` 基于 [Yonsm/MiService](https://github.com/Yonsm/MiService) 3.0.1，许可证见 [MISERVICE-LICENSE](deploy/router/MISERVICE-LICENSE)。Edge 客户端通过 PyPI 安装，来源为 [rany2/edge-tts](https://github.com/rany2/edge-tts)。微软调整在线朗读协议时，可能需要更新客户端。
