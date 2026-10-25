# XiaoGPT Router

面向 OpenWrt / iStoreOS Docker 路由器的 XiaoGPT 部署版本，增加免费的 Edge 中文语音连续播报。

基于 [yihong0618/xiaogpt](https://github.com/yihong0618/xiaogpt)，源码基线为 [`4258b0b53cf6af062f1a01af993d47a161382a0f`](https://github.com/yihong0618/xiaogpt/commit/4258b0b53cf6af062f1a01af993d47a161382a0f)。保留上游 MIT 许可证；原始说明见 [README.upstream.md](README.upstream.md)。

## 本版本的改动

- 固定 `edge-tts==7.3.1`，修复旧版请求校验导致的 HTTP 403。
- Edge 音频边生成边传给音箱，一次回答只启动一个播放 URL，提前合成后续文字。
- 音频缓存支持多个请求独立读取及 Range 重试，并处理播放结束后的重复请求。
- 开启 `mute_xiaoai` 时，发现有效提问就直接暂停小爱，不再先查询播放状态；保留“正在问……”提示和原有约一秒的轮询等待。
- 保留小爱原声选项，对较长原声播报分段并检查指令返回结果。
- 修复 Python 3.14 启动入口，以及小米短信验证后的确认跳转。

已在 x86_64 iStoreOS、Intel J4125 / 4 GB 内存、小爱音箱 Play 增强版 L05C 上验证。实际固件支持外部 MP3 和渐进式 HTTP 音频播放；其他型号仍需验证。

默认使用“晓晓”中文女声、流式回答、Edge 语音音量 `+40%`。本版本不修改音箱自身音量。Edge 需要联网；聊天模型仍使用用户自己的 API，费用取决于聊天服务商。

## Docker 部署

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
  -v "$PWD/tests:/tests:ro" \
  --entrypoint /app/.venv/bin/python xiaogpt-router:local \
  -m unittest discover -s /tests -v
```

GitHub Actions 仅提供手动检查，不自动发布 Docker Hub 镜像或 PyPI 包。

## 配置与来源

仓库只提供空凭据模板。实际配置、`.mi.token`、Cookie、环境文件、日志和音频不应提交到 Git。

`deploy/router/miaccount.py` 基于 [Yonsm/MiService](https://github.com/Yonsm/MiService) 3.0.1，许可证见 [MISERVICE-LICENSE](deploy/router/MISERVICE-LICENSE)。Edge 客户端通过 PyPI 安装，来源为 [rany2/edge-tts](https://github.com/rany2/edge-tts)。微软调整在线朗读协议时，可能需要更新客户端。
