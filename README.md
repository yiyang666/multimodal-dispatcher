# 6000 Ada Multimodal Dispatcher

面向单机、单 GPU、显存有限场景的本地多模态模型调度器。它对客户端暴露一个稳定的 OpenAI 兼容入口，根据请求中的模型 ID 唤醒或切换文本、图像和视频后端。

License: [MIT](LICENSE)

## 当前范围

- 已实现：vLLM 文本模型的唤醒、休眠、容器启动、健康检查和 `/v1/chat/completions` 反向代理。
- 已实现：模型注册表、GPU 全局互斥锁、管理 API、OpenAI 风格 `/v1/models`。
- 已实现：可配置的空闲释放；默认 10 分钟无请求后，vLLM 停止容器、ComfyUI 调用 `/free`。
- 已实现：ComfyUI 生命周期管理，以及基于固定工作流的 `/v1/images/generations`（Qwen-Image FP8 模板）。
- 暂未实现：把自然语言图片请求自动编译为任意 ComfyUI 工作流；视频后端适配。

详细设计见 [docs/architecture.md](docs/architecture.md)，部署与日常管理见 [docs/operations.md](docs/operations.md)。

## 快速开始

```bash
cp config/models.example.yaml config/models.yaml
cp .env.example .env
# 编辑 .env：DISPATCHER_BIND_IP、DISPATCHER_ADMIN_TOKEN
docker compose up -d --build
curl http://127.0.0.1:8000/health
```

生产环境请将 Dispatcher 绑定至私有网络（例如 Tailscale）；vLLM 和 ComfyUI 后端仅绑定 `127.0.0.1`。

后端容器示例见 [deploy/](deploy/)（vLLM / ComfyUI）。

## 对外模型 ID

| ID | 角色 | 当前状态 |
| --- | --- | --- |
| `agent-6000ada` | 通用/视觉理解 | 已配置示例 |
| `coder-6000ada` | 编程 | 预留 |
| `image-6000ada` | 图像生成（Qwen-Image） | 示例已启用，需自备 ComfyUI 与权重 |
| `video-6000ada` | 视频生成 | 预留 |

## 安全边界

Dispatcher 挂载 Docker socket 以控制后端容器，等同于拥有本机 Docker 管理权限。必须只在受信任的网络中暴露，并为 `/admin/*` 设置强随机令牌。

## License

MIT © 2026 yiyang666
